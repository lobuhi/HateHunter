#!/usr/bin/env python3
import argparse
import json
import os
import re
import subprocess
import sys
import glob
import openai
import requests
from urllib.parse import urlparse, parse_qs

###############################
# Helper to parse comma-separated keywords
###############################

def parse_keywords(value):
    """
    Splits the input string by commas and strips whitespace from each keyword.
    Returns a list of keywords.
    """
    return [kw.strip() for kw in value.split(',') if kw.strip()]

###############################
# Video ID extraction and duplicate check helpers
###############################

def extract_video_id(url):
    """
    Extracts the video ID from a YouTube URL.
    """
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if 'v' in qs:
        return qs['v'][0]
    return os.path.basename(parsed.path)

def check_videos_already_processed(project, video_urls):
    """
    If the project JSON exists, check whether any of the provided video IDs
    are already present. If so, print a message and exit.
    """
    project_json = f"{project}.json"
    if not os.path.exists(project_json):
        return
    try:
        with open(project_json, "r", encoding="utf-8") as f:
            existing_results = json.load(f)
    except Exception as e:
        print(f"Error reading existing project JSON: {e}")
        return
    processed_ids = {res["Filename"].split('.')[0] for res in existing_results if "Filename" in res}
    for url in video_urls:
        vid = extract_video_id(url)
        if vid in processed_ids:
            print(f"Video {vid} is already processed in project '{project}'. Exiting.")
            sys.exit(0)

###############################
# Conversion functions (from convertsrt.py)
###############################

def time_to_seconds(timestamp):
    """Convert an SRT timestamp (HH:MM:SS,mmm) into seconds (float)."""
    h, m, s_ms = timestamp.split(":")
    s, ms = s_ms.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0

def clean_text(text):
    """
    Remove any stray SRT timestamp lines from text.
    """
    timestamp_pattern = re.compile(r'\d{2}:\d{2}:\d{2},\d{3}\s*-->\s*\d{2}:\d{2}:\d{2},\d{3}')
    cleaned_lines = []
    for line in text.splitlines():
        if not timestamp_pattern.search(line):
            cleaned_lines.append(line)
    return " ".join(cleaned_lines).strip()

def parse_srt(srt_text):
    """
    Parse SRT content and return a list of tuples:
    (index, start_time_in_seconds, cleaned_text)
    """
    pattern = re.compile(
        r"(\d+)\s*\n"                           # Subtitle index
        r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*"    # Start time
        r"(\d{2}:\d{2}:\d{2},\d{3})\s*\n"         # End time
        r"(.*?)(?=\n\s*\n|\Z)",                   # Text (non-greedy)
        re.DOTALL
    )
    blocks = []
    for match in pattern.finditer(srt_text):
        index = int(match.group(1))
        start_time_str = match.group(2)
        raw_text = match.group(4).replace("\n", " ").strip()
        text = clean_text(raw_text)
        start_time_sec = time_to_seconds(start_time_str)
        blocks.append((index, start_time_sec, text))
    return blocks

def longest_overlap(a, b):
    """Return the length of the longest suffix of a that is a prefix of b."""
    max_overlap = 0
    max_possible = min(len(a), len(b))
    for i in range(1, max_possible + 1):
        if a[-i:] == b[:i]:
            max_overlap = i
    return max_overlap

def merge_texts(texts):
    """
    Merge a list of text strings by appending only the non-repeated parts.
    """
    merged = ""
    for text in texts:
        if not merged:
            merged = text
        else:
            if text in merged:
                continue
            overlap_len = longest_overlap(merged, text)
            if overlap_len > 0:
                addition = text[overlap_len:].strip()
                if addition:
                    merged += " " + addition
            else:
                merged += " " + text
    return merged

def group_blocks(blocks, threshold=30):
    """
    Group subtitle blocks into chunks where the difference between the first block
    in a group and any subsequent block is at most 'threshold' seconds.
    """
    groups = []
    current_group = []
    group_start = None
    for block in sorted(blocks, key=lambda x: x[1]):
        _, start_sec, _ = block
        if group_start is None:
            group_start = start_sec
        if start_sec - group_start <= threshold:
            current_group.append(block)
        else:
            groups.append((group_start, current_group))
            current_group = [block]
            group_start = start_sec
    if current_group:
        groups.append((group_start, current_group))
    return groups

def process_srt(srt_text, threshold=30):
    """
    Process the SRT text:
      - Parse and clean each block.
      - Group blocks approximately every 'threshold' seconds.
      - Merge texts in each group.
    Returns a list of tuples (group_start_time, merged_text).
    """
    blocks = parse_srt(srt_text)
    grouped = group_blocks(blocks, threshold)
    merged_groups = []
    for group_start, blocks_in_group in grouped:
        texts = [text for (_, _, text) in sorted(blocks_in_group, key=lambda x: x[1])]
        merged_text = merge_texts(texts)
        merged_groups.append((int(group_start), merged_text))
    return merged_groups

def convert_srt_file(srt_file, threshold=30):
    """
    Read an SRT file, process it, and return the converted text.
    """
    with open(srt_file, "r", encoding="utf-8") as f:
        srt_text = f.read()
    merged_groups = process_srt(srt_text, threshold)
    output_lines = []
    for timestamp, text in merged_groups:
        output_lines.append(str(timestamp))
        output_lines.append(text)
        output_lines.append("")
    return "\n".join(output_lines)

###############################
# YouTube subtitle download functions
###############################

def get_video_list(channel_url):
    """
    Retrieve the video list for the channel and adjust missing timestamps.
    Saves the list as videos.json.
    """
    playlist_url = channel_url.rstrip("/") + "/videos"
    print("Retrieving video list from:", playlist_url)
    try:
        result = subprocess.run(["yt-dlp", "-J", "--flat-playlist", playlist_url],
                                capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        print("Error retrieving video list:", e)
        sys.exit(1)
    data = json.loads(result.stdout)
    entries = data.get("entries", [])
    video_list = []
    total = len(entries)
    for idx, entry in enumerate(entries):
        video_id = entry.get("id")
        title = entry.get("title", "")
        timestamp = entry.get("timestamp")
        if timestamp is None:
            timestamp = total - idx
        video_list.append({"id": video_id, "title": title, "timestamp": timestamp})
    with open("videos.json", "w", encoding="utf-8") as f:
        json.dump(video_list, f, ensure_ascii=False, indent=4)
    print("Saved video list to videos.json")
    return video_list

def download_subtitles(video_list, language):
    """
    Download auto-generated subtitles (in SRT format) for each video in the list.
    """
    for video in video_list:
        video_id = video["id"]
        print(f"Downloading subtitles for video ID: {video_id}")
        url = f"https://www.youtube.com/watch?v={video_id}"
        try:
            subprocess.run(["yt-dlp", "--skip-download", "--sub-lang", language,
                            "--write-auto-subs", "--convert-subs", "srt",
                            "-o", f"{video_id}.%(ext)s", url], check=True)
        except subprocess.CalledProcessError as e:
            print(f"Error downloading subtitles for {video_id}: {e}")

def download_subtitles_for_video(video_url, language):
    """
    Download auto-generated subtitles (in SRT format) for a single video.
    """
    print("Downloading subtitles for video:", video_url)
    try:
        subprocess.run(["yt-dlp", "--skip-download", "--sub-lang", language,
                        "--write-auto-subs", "--convert-subs", "srt",
                        "-o", "%(id)s.%(ext)s", video_url], check=True)
    except subprocess.CalledProcessError as e:
        print("Error downloading subtitles for video:", e)

###############################
# Moderation helper using requests
###############################

def moderate_text(text):
    """
    Use the OpenAI Moderation API via a direct HTTP request.
    """
    url = "https://api.openai.com/v1/moderations"
    headers = {
        "Authorization": f"Bearer {openai.api_key}",
        "Content-Type": "application/json"
    }
    payload = {"input": text, "model": "text-moderation-latest"}
    response = requests.post(url, headers=headers, json=payload)
    if response.status_code != 200:
        raise Exception(f"Moderation API request failed with status code {response.status_code}: {response.text}")
    return response.json()

###############################
# Highlight helper
###############################

def highlight_text(text, keywords):
    """
    For each keyword provided, highlight (wrap in a span with yellow background)
    any occurrence (as a substring, case-insensitive) in the text.
    """
    if not keywords:
        return text
    for kw in keywords:
        pattern = re.compile(re.escape(kw), re.IGNORECASE)
        text = pattern.sub(lambda m: '<span style="background-color: yellow;">{}</span>'.format(m.group(0)), text)
    return text

###############################
# Detector functions
###############################

def analyze_file(file_path, keywords):
    """
    Analyze the converted file (in .s30 format) for problematic content.
    If keywords are provided, only process lines containing any keyword.
    If keywords list is empty, process every line.
    Returns a list of moderation results.
    """
    print(f"Analyzing file: {file_path}")
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.readlines()
    except Exception as e:
        print(f"Error reading file {file_path}: {e}")
        return []
    results = []
    filename = os.path.basename(file_path)
    video_id = filename.split('.')[0]
    for i, line in enumerate(content):
        if keywords and not any(keyword.lower() in line.lower() for keyword in keywords):
            continue
        timestamp = None
        if i > 0:
            prev_line = content[i - 1].strip()
            try:
                timestamp = float(prev_line)
            except ValueError:
                timestamp = None
        try:
            moderation_response = moderate_text(line)
        except Exception as e:
            print(f"Error connecting to OpenAI Moderation API: {e}")
            continue

        moderation_result = moderation_response["results"][0]
        flagged = moderation_result.get("flagged", False)
        categories = [cat for cat, val in moderation_result.get("categories", {}).items() if val]
        if flagged:
            youtube_url = None
            if timestamp is not None:
                youtube_url = f"https://www.youtube.com/watch?v={video_id}&t={int(timestamp)}"
            results.append({
                "Filename": filename,
                "Timestamp": timestamp,
                "Texto": line.strip(),
                "Categorías": ", ".join(categories),
                "YouTubeURL": youtube_url
            })
    return results

def merge_analysis_results(keywords, project):
    """
    Analyze all .s30 files in the current directory, merge their moderation results
    with any existing project file (filtering duplicates by Filename, Timestamp, and Texto),
    write a single merged JSON and HTML file, and then delete all *.srt and *.s30 files.
    """
    s30_files = glob.glob("*.s30")
    new_results = []
    for file in s30_files:
        file_results = analyze_file(file, keywords)
        new_results.extend(file_results)
    project_json = f"{project}.json"
    project_html = f"{project}.html"
    if os.path.exists(project_json):
        try:
            with open(project_json, "r", encoding="utf-8") as f:
                existing_results = json.load(f)
        except Exception as e:
            print(f"Error reading existing project JSON: {e}")
            existing_results = []
    else:
        existing_results = []
    combined = existing_results + new_results
    unique_dict = {}
    for item in combined:
        key_tuple = (item["Filename"], item.get("Timestamp"), item["Texto"])
        unique_dict[key_tuple] = item
    merged_results = list(unique_dict.values())
    merged_results.sort(key=lambda x: (x["Filename"], x.get("Timestamp") if x.get("Timestamp") is not None else 0))
    for item in merged_results:
        item["Texto"] = highlight_text(item["Texto"], keywords)
    with open(project_json, "w", encoding="utf-8") as f:
        json.dump(merged_results, f, ensure_ascii=False, indent=4)
    html_content = '''<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Moderation Results - {project}</title>
    <style>
        table {{ border-collapse: collapse; width: 100%; }}
        th, td {{ padding: 8px; border: 1px solid #ccc; }}
        th {{ background-color: #f2f2f2; }}
    </style>
</head>
<body>
    <h1>Moderation Results - {project}</h1>
    <table id="resultsTable">
        <thead>
            <tr>
                <th>Filename</th>
                <th>Timestamp</th>
                <th>Texto</th>
                <th>Categorías</th>
                <th>YouTubeURL</th>
            </tr>
        </thead>
        <tbody>
        </tbody>
    </table>
    <script>
        const data = {json_data};
        const tableBody = document.getElementById('resultsTable').getElementsByTagName('tbody')[0];
        data.forEach(item => {{
            const row = tableBody.insertRow();
            let displayFilename = item.Filename.split('.')[0];
            const filenameCell = row.insertCell();
            filenameCell.textContent = displayFilename;
            const timestampCell = row.insertCell();
            timestampCell.textContent = item.Timestamp;
            const textoCell = row.insertCell();
            textoCell.innerHTML = item.Texto;
            const categoriasCell = row.insertCell();
            categoriasCell.textContent = item["Categorías"];
            const urlCell = row.insertCell();
            if (item.YouTubeURL) {{
                const link = document.createElement('a');
                link.href = item.YouTubeURL;
                link.textContent = item.YouTubeURL;
                link.target = "_blank";
                urlCell.appendChild(link);
            }} else {{
                urlCell.textContent = 'N/A';
            }}
        }});
    </script>
</body>
</html>'''.format(project=project, json_data=json.dumps(merged_results, ensure_ascii=False, indent=4))
    with open(project_html, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"Merged moderation results exported to {project_json} and {project_html}")
    for pattern in ["*.srt", "*.s30"]:
        for f in glob.glob(pattern):
            try:
                os.remove(f)
            except Exception as e:
                print(f"Error deleting file {f}: {e}")

###############################
# Helper functions for processing files
###############################

def convert_all_srt_files(threshold):
    """
    Convert all SRT files in the current directory to .s30 files.
    """
    srt_files = glob.glob("*.srt")
    if not srt_files:
        print("No .srt files found for conversion.")
        return
    for srt_file in srt_files:
        print(f"Converting {srt_file}...")
        output_text = convert_srt_file(srt_file, threshold)
        base = os.path.splitext(srt_file)[0]
        out_file = base + ".s30"
        with open(out_file, "w", encoding="utf-8") as f:
            f.write(output_text)
        print(f"Converted file saved as {out_file}")

###############################
# Main entry point
###############################

def main():
    parser = argparse.ArgumentParser(description="Merged HateDetector Tool")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--channel", type=str, help="YouTube channel URL (process entire channel)")
    group.add_argument("--video", type=str, nargs="+", help="YouTube video URL(s) to process")
    parser.add_argument("--language", type=str, default="en", help="Language for subtitles (default: en)")
    parser.add_argument("--openai-api-key", type=str, help="OpenAI API key for content moderation")
    parser.add_argument("--threshold", type=int, default=30, help="Time threshold (in seconds) for SRT grouping (default: 30)")
    parser.add_argument("--skip-convert", action="store_true", help="Skip converting SRT files")
    parser.add_argument("--skip-analyze", action="store_true", help="Skip analyzing converted files")
    parser.add_argument("--keywords", "-k", type=parse_keywords, default=[],
                        help="Comma-separated list of keywords to search for and highlight in the text. Example: \"islam, jew, black\"")
    parser.add_argument("--project", "-p", type=str, default="merged_moderation",
                        help="Project name to use as base for the merged JSON and HTML file. New data will be merged with existing data if the file exists.")
    
    args = parser.parse_args()
    
    # Set API key from environment variable if available, then from command line argument, otherwise ask user.
    if not args.skip_analyze:
        env_key = os.environ.get("OPENAI_API_KEY")
        if env_key:
            openai.api_key = env_key
        elif args.openai_api_key:
            openai.api_key = args.openai_api_key
        else:
            openai.api_key = input("Please enter your OpenAI API key: ")

    # In video mode, check for duplicates before processing.
    if args.video:
        check_videos_already_processed(args.project, args.video)
        for video_url in args.video:
            download_subtitles_for_video(video_url, args.language)
    elif args.channel:
        video_list = get_video_list(args.channel)
        download_subtitles(video_list, args.language)
    
    if not args.skip_convert:
        convert_all_srt_files(args.threshold)
    
    if not args.skip_analyze:
        merge_analysis_results(args.keywords, args.project)

if __name__ == "__main__":
    main()

