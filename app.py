import os
import uuid
import glob
import json
import re
import shutil
import subprocess
import tempfile
import threading
from flask import Flask, request, jsonify, send_file, render_template

app = Flask(__name__)
# Scratch dir: yt-dlp works here, and "download to my device" jobs are served from here.
DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")
# Library dir: target of "save to server" jobs (bind-mounted to the Jellyfin videos folder).
LIBRARY_DIR = os.environ.get("LIBRARY_DIR", os.path.join(os.path.dirname(__file__), "library"))
DOWNLOAD_TIMEOUT = int(os.environ.get("DOWNLOAD_TIMEOUT", 1800))
TRANSCRIBE_TIMEOUT = int(os.environ.get("TRANSCRIBE_TIMEOUT", 3600))
# Falls back to plain yt-dlp (and occasional 429s on captions) if not set.
POT_PROVIDER_URL = os.environ.get("POT_PROVIDER_URL", "")
WHISPER_MODEL_SIZE = os.environ.get("WHISPER_MODEL", "small")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(LIBRARY_DIR, exist_ok=True)

jobs = {}
_whisper_model = None  # lazy-loaded singleton so app startup doesn't pay for it


def ytdlp_extra_args():
    """Extra yt-dlp flags shared by every invocation (currently just the PO token sidecar)."""
    if not POT_PROVIDER_URL:
        return []
    return ["--extractor-args", f"youtubepot-bgutilhttp:base_url={POT_PROVIDER_URL}"]


def get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
    return _whisper_model


def srt_timestamp(seconds):
    ms = round(seconds * 1000)
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def safe_filename(title, fallback):
    """Make a title safe as a filename on Windows/Linux (bind mount lives on NTFS)."""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", title or "")
    name = re.sub(r"\s+", " ", name).strip()[:100].strip(" .")
    return name or fallback


def unique_path(directory, name, ext):
    """Return directory/name+ext, appending ' (2)', ' (3)'... if it already exists."""
    path = os.path.join(directory, f"{name}{ext}")
    n = 2
    while os.path.exists(path):
        path = os.path.join(directory, f"{name} ({n}){ext}")
        n += 1
    return path


def parse_ytdlp_json(stdout):
    """Parse yt-dlp JSON output.

    With ``-j`` yt-dlp prints one JSON object per line. Some extractors
    emit multiple videos even with ``--no-playlist``, so stdout contains
    several objects and a plain ``json.loads`` raises "Extra data".
    Return the first valid object.
    """
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        return json.loads(line)
    raise ValueError("yt-dlp returned no data")


def fetch_captions(url, tmp_dir, job_id):
    """Try to fetch existing captions (creator-uploaded or YouTube auto-generated) as SRT.

    Returns the .srt path on success, or None if the source has no captions at all
    (which is a normal outcome, not an error).
    """
    out_template = os.path.join(tmp_dir, f"{job_id}.%(ext)s")
    cmd = (
        ["yt-dlp", "--no-playlist", "--skip-download",
         "--write-subs", "--write-auto-subs", "--sub-langs", "en",
         "--convert-subs", "srt", "-o", out_template]
        + ytdlp_extra_args() + [url]
    )
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return None

    srts = sorted(glob.glob(os.path.join(tmp_dir, f"{job_id}.*.srt")))
    if not srts:
        return None
    # Prefer a manually-uploaded track (yt-dlp suffixes auto ones the same way,
    # so this is a best-effort pick, not a guarantee) and clean up the rest.
    chosen = srts[0]
    for f in srts[1:]:
        try:
            os.remove(f)
        except OSError:
            pass
    return chosen


def transcribe_with_whisper(video_path, tmp_dir, job_id):
    """Fall back to local speech-to-text when the source has no captions.

    Extracts audio with ffmpeg, runs it through faster-whisper, and returns
    (srt_text, txt_text).
    """
    audio_path = os.path.join(tmp_dir, f"{job_id}.wav")
    ffmpeg_cmd = ["ffmpeg", "-y", "-i", video_path, "-vn", "-ac", "1", "-ar", "16000", audio_path]
    subprocess.run(ffmpeg_cmd, capture_output=True, text=True, timeout=TRANSCRIBE_TIMEOUT, check=True)

    model = get_whisper_model()
    segments, _info = model.transcribe(audio_path, beam_size=5)

    srt_lines = []
    txt_lines = []
    for i, seg in enumerate(segments, start=1):
        text = seg.text.strip()
        txt_lines.append(text)
        srt_lines.append(str(i))
        srt_lines.append(f"{srt_timestamp(seg.start)} --> {srt_timestamp(seg.end)}")
        srt_lines.append(text)
        srt_lines.append("")

    return "\n".join(srt_lines), " ".join(txt_lines)


def download_audio_only(url, tmp_dir, job_id):
    """Grab just the audio track (for Whisper) — much smaller/faster than a full video."""
    out_template = os.path.join(tmp_dir, f"{job_id}-audio.%(ext)s")
    cmd = (
        ["yt-dlp", "--no-playlist", "-x", "--audio-format", "mp3", "-o", out_template]
        + ytdlp_extra_args() + [url]
    )
    subprocess.run(cmd, capture_output=True, text=True, timeout=DOWNLOAD_TIMEOUT, check=True)
    files = glob.glob(os.path.join(tmp_dir, f"{job_id}-audio.*"))
    if not files:
        raise RuntimeError("Audio download completed but no file was found")
    return files[0]


def run_transcript_only(job_id, url):
    """Captions/transcript with no video (or audio) ever saved to disk.

    Tries existing captions first (no download at all); only falls back to
    downloading just the audio track (never the video) for Whisper, and that
    audio is discarded once transcribed.
    """
    job = jobs[job_id]
    dest = job.get("dest", "browser")
    safe_title = safe_filename(job.get("title", ""), job_id)
    target_dir = LIBRARY_DIR if dest == "server" else DOWNLOAD_DIR
    scratch = os.path.join(DOWNLOAD_DIR, f"{job_id}-scratch")
    os.makedirs(scratch, exist_ok=True)
    try:
        srt_path = fetch_captions(url, scratch, job_id)
        if srt_path:
            srt_text = open(srt_path, encoding="utf-8", errors="ignore").read()
            txt_text = " ".join(
                line.strip() for line in srt_text.splitlines()
                if line.strip() and "-->" not in line and not line.strip().isdigit()
            )
            job["transcript_source"] = "captions"
        else:
            audio_path = download_audio_only(url, scratch, job_id)
            srt_text, txt_text = transcribe_with_whisper(audio_path, scratch, job_id)
            job["transcript_source"] = "whisper"

        final_srt = unique_path(target_dir, safe_title, ".srt")
        final_txt = unique_path(target_dir, safe_title, ".txt")
        with open(final_srt, "w", encoding="utf-8") as f:
            f.write(srt_text)
        with open(final_txt, "w", encoding="utf-8") as f:
            f.write(txt_text)

        job["transcript_srt"] = final_srt
        job["transcript_txt"] = final_txt
        job["transcript_status"] = "done"
        job["file"] = final_txt
        job["filename"] = os.path.basename(final_txt)
        job["status"] = "done"
    except subprocess.CalledProcessError as e:
        err = (e.stderr or "").strip().split("\n")[-1] or str(e)
        job["status"] = "error"
        job["transcript_status"] = "error"
        job["error"] = err
        job["transcript_error"] = err
    except Exception as e:
        job["status"] = "error"
        job["transcript_status"] = "error"
        job["error"] = str(e)
        job["transcript_error"] = str(e)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def generate_transcript(job, url, video_path, library_dir, safe_title):
    """Best-effort: caption fetch first, then Whisper. Never raises — failures are
    recorded on the job so the video download itself is unaffected."""
    job["transcript_status"] = "running"
    try:
        with tempfile.TemporaryDirectory(dir=DOWNLOAD_DIR) as tmp_dir:
            job_id = job["job_id"]
            srt_path = fetch_captions(url, tmp_dir, job_id)
            if srt_path:
                srt_text = open(srt_path, encoding="utf-8", errors="ignore").read()
                txt_text = " ".join(
                    line.strip() for line in srt_text.splitlines()
                    if line.strip() and "-->" not in line and not line.strip().isdigit()
                )
                job["transcript_source"] = "captions"
            else:
                srt_text, txt_text = transcribe_with_whisper(video_path, tmp_dir, job_id)
                job["transcript_source"] = "whisper"

            final_srt = unique_path(library_dir, safe_title, ".srt")
            final_txt = unique_path(library_dir, safe_title, ".txt")
            with open(final_srt, "w", encoding="utf-8") as f:
                f.write(srt_text)
            with open(final_txt, "w", encoding="utf-8") as f:
                f.write(txt_text)

        job["transcript_srt"] = final_srt
        job["transcript_txt"] = final_txt
        job["transcript_status"] = "done"
    except Exception as e:
        job["transcript_status"] = "error"
        job["transcript_error"] = str(e)


def run_download(job_id, url, format_choice, format_id):
    job = jobs[job_id]
    out_template = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")

    cmd = ["yt-dlp", "--no-playlist", "-o", out_template] + ytdlp_extra_args()

    if format_choice == "audio":
        cmd += ["-x", "--audio-format", "mp3"]
    elif format_id:
        cmd += ["-f", f"{format_id}+bestaudio/best", "--merge-output-format", "mp4"]
    else:
        cmd += ["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"]

    cmd.append(url)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=DOWNLOAD_TIMEOUT)
        if result.returncode != 0:
            job["status"] = "error"
            job["error"] = result.stderr.strip().split("\n")[-1]
            return

        files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))
        if not files:
            job["status"] = "error"
            job["error"] = "Download completed but no file was found"
            return

        if format_choice == "audio":
            target = [f for f in files if f.endswith(".mp3")]
            chosen = target[0] if target else files[0]
        else:
            target = [f for f in files if f.endswith(".mp4")]
            chosen = target[0] if target else files[0]

        for f in files:
            if f != chosen:
                try:
                    os.remove(f)
                except OSError:
                    pass

        ext = os.path.splitext(chosen)[1]
        safe_title = safe_filename(job.get("title", ""), job_id)

        if job.get("dest") == "server":
            # Move into the library under its real title so Jellyfin shows a proper name.
            final = unique_path(LIBRARY_DIR, safe_title, ext)
            shutil.move(chosen, final)
            job["file"] = final
            job["filename"] = os.path.basename(final)
            job["status"] = "done"
            if job.get("transcript"):
                # Runs synchronously in this same background thread — the job stays
                # "downloading" in the UI's eyes only via transcript_status, video is
                # already marked done above so "Saved to server" shows immediately.
                generate_transcript(job, url, final, LIBRARY_DIR, safe_title)
        else:
            job["file"] = chosen
            job["filename"] = f"{safe_title}{ext}"
            job["status"] = "done"
    except subprocess.TimeoutExpired:
        job["status"] = "error"
        job["error"] = f"Download timed out ({DOWNLOAD_TIMEOUT // 60} min limit)"
        for f in glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*")):
            try:
                os.remove(f)
            except OSError:
                pass
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    cmd = ["yt-dlp", "--no-playlist", "-j"] + ytdlp_extra_args() + [url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return jsonify({"error": result.stderr.strip().split("\n")[-1]}), 400

        info = parse_ytdlp_json(result.stdout)

        # Build quality options — keep best format per resolution
        best_by_height = {}
        for f in info.get("formats", []):
            height = f.get("height")
            if height and f.get("vcodec", "none") != "none":
                tbr = f.get("tbr") or 0
                if height not in best_by_height or tbr > (best_by_height[height].get("tbr") or 0):
                    best_by_height[height] = f

        formats = []
        for height, f in best_by_height.items():
            formats.append({
                "id": f["format_id"],
                "label": f"{height}p",
                "height": height,
            })
        formats.sort(key=lambda x: x["height"], reverse=True)

        return jsonify({
            "title": info.get("title", ""),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration"),
            "uploader": info.get("uploader", ""),
            "formats": formats,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching video info"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/playlist", methods=["POST"])
def get_playlist_info():
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    cmd = ["yt-dlp", "--flat-playlist", "-J"] + ytdlp_extra_args() + [url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return jsonify({"error": result.stderr.strip().split("\n")[-1]}), 400

        info = json.loads(result.stdout)
        entries = info.get("entries", [])
        urls = [entry.get("url") for entry in entries if entry.get("url")]
        return jsonify({"urls": urls})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching playlist info"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json
    url = data.get("url", "").strip()
    format_choice = data.get("format", "video")
    format_id = data.get("format_id")
    title = data.get("title", "")
    dest = "server" if data.get("dest") == "server" else "browser"
    transcript_only = format_choice == "transcript"
    # Sidecar transcripts (alongside a video) only make sense for files landing in the library.
    transcript = bool(data.get("transcript")) and dest == "server" and not transcript_only

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    job_id = uuid.uuid4().hex[:10]
    jobs[job_id] = {
        "job_id": job_id, "status": "downloading", "url": url, "title": title,
        "dest": dest, "transcript": transcript,
        "transcript_status": "pending" if (transcript or transcript_only) else None,
    }

    if transcript_only:
        thread = threading.Thread(target=run_transcript_only, args=(job_id, url))
    else:
        thread = threading.Thread(target=run_download, args=(job_id, url, format_choice, format_id))
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def check_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status": job["status"],
        "error": job.get("error"),
        "filename": job.get("filename"),
        "transcript_status": job.get("transcript_status"),
        "transcript_error": job.get("transcript_error"),
        "transcript_source": job.get("transcript_source"),
    })


@app.route("/api/file/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done" or job.get("dest") == "server":
        return jsonify({"error": "File not ready"}), 404
    return send_file(job["file"], as_attachment=True, download_name=job["filename"])


@app.route("/api/transcript/<job_id>")
def get_transcript(job_id):
    job = jobs.get(job_id)
    if not job or job.get("transcript_status") != "done":
        return jsonify({"error": "Transcript not ready"}), 404
    path = job.get("transcript_txt")
    if not path or not os.path.exists(path):
        return jsonify({"error": "Transcript file is missing"}), 404
    with open(path, encoding="utf-8", errors="ignore") as f:
        text = f.read()
    return jsonify({"text": text, "filename": os.path.basename(path)})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "127.0.0.1")
    app.run(host=host, port=port)
