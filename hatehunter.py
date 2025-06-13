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
import time
import hashlib
from threading import Lock
from urllib.parse import urlparse, parse_qs
from openai import OpenAI
from jinja2 import Environment, FileSystemLoader
from datetime import datetime

# Importar las nuevas dependencias para SQLite
from database import db
from models import Project, Video, SubtitleFlag, CommentFlag
import socketio

# Global instance
api_manager = None

# Queue file path (same as server.py)
QUEUE_FILE = "hatehunter.tmp"

class ModerationAPIManager:
    def __init__(self, max_requests_per_second=10):
        self.max_requests_per_second = max_requests_per_second
        self.request_interval = 1.0 / max_requests_per_second
        self.last_request_time = 0
        self.request_lock = Lock()
        self.moderation_cache = {}
        self.cache_hits = 0
        self.api_calls = 0
    
    def _get_text_hash(self, text):
        return hashlib.md5(text.encode('utf-8')).hexdigest()
    
    def _wait_for_rate_limit(self):
        with self.request_lock:
            current_time = time.time()
            time_since_last_request = current_time - self.last_request_time
            
            if time_since_last_request < self.request_interval:
                sleep_time = self.request_interval - time_since_last_request
                print(f"⏳ Rate limiting: waiting {sleep_time:.2f}s")
                time.sleep(sleep_time)
            
            self.last_request_time = time.time()
    
    def moderate_text(self, text):
        text_clean = text.strip()
        if not text_clean:
            return {"results": [{"flagged": False, "categories": {}}]}
        
        text_hash = self._get_text_hash(text_clean)
        if text_hash in self.moderation_cache:
            self.cache_hits += 1
            print(f"💾 Cache hit for text hash: {text_hash[:8]}... (total hits: {self.cache_hits})")
            return self.moderation_cache[text_hash]
        
        self._wait_for_rate_limit()
        self.api_calls += 1
        
        try:
            print(f"🔍 API call #{self.api_calls} for text hash: {text_hash[:8]}...")
            
            url = "https://api.openai.com/v1/moderations"
            headers = {
                "Authorization": f"Bearer {openai.api_key}",
                "Content-Type": "application/json"
            }
            payload = {"input": text_clean, "model": "omni-moderation-latest"}
            
            response = requests.post(url, headers=headers, json=payload, timeout=30)
            
            if response.status_code != 200:
                raise Exception(f"Moderation API request failed with status code {response.status_code}: {response.text}")
            
            result = response.json()
            self.moderation_cache[text_hash] = result
            print(f"✅ API call successful, result cached")
            
            return result
            
        except Exception as e:
            print(f"❌ Error in moderation API call: {e}")
            return {"results": [{"flagged": False, "categories": {}}]}
    
    def moderate_comment_with_client(self, comment_text, client):
        moderation_response = self.moderate_text(comment_text)
        result = moderation_response["results"][0]
        categories_dict = result.get("categories", {})
        hate_categories = [cat for cat, flagged in categories_dict.items() if flagged and "hate" in cat.lower()]
        return hate_categories
    
    def print_stats(self):
        total_requests = self.api_calls + self.cache_hits
        cache_hit_rate = (self.cache_hits / total_requests * 100) if total_requests > 0 else 0
        
        print(f"\n📊 Moderation API Statistics:")
        print(f"   - Total texts processed: {total_requests}")
        print(f"   - API calls made: {self.api_calls}")
        print(f"   - Cache hits: {self.cache_hits}")
        print(f"   - Cache hit rate: {cache_hit_rate:.1f}%")
        print(f"   - Estimated time saved: {self.cache_hits * 0.1:.1f}s")

def parse_keywords(value):
    return [kw.strip() for kw in value.split(',') if kw.strip()]

def extract_video_id(url_or_id):
    if not url_or_id:
        return None
    
    if 'youtube.com' in url_or_id or 'youtu.be' in url_or_id:
        pattern = r"(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})"
        m = re.search(pattern, url_or_id)
        if m:
            return m.group(1)
        
        parsed = urlparse(url_or_id)
        qs = parse_qs(parsed.query)
        if 'v' in qs:
            return qs['v'][0]
        return os.path.basename(parsed.path)
    
    return url_or_id

def ensure_thumbnail(video_id):
    folder = "thumbnails"
    os.makedirs(folder, exist_ok=True)
    thumb_path = os.path.join(folder, f"{video_id}.jpg")
    if not os.path.exists(thumb_path):
        url = f"https://img.youtube.com/vi/{video_id}/mqdefault.jpg"
        try:
            response = requests.get(url, timeout=10)
            if response.status_code == 200:
                with open(thumb_path, "wb") as f:
                    f.write(response.content)
                print(f"✅ Downloaded thumbnail for video {video_id}")
        except Exception as e:
            print(f"❌ Failed to download thumbnail for {video_id}: {e}")

def download_comments(video_id):
    url = f"https://www.youtube.com/watch?v={video_id}"
    
    cmd = [
        "yt-dlp",
        "--write-comments",
        "--write-info-json",
        "--skip-download",
        "-o", "%(id)s.%(ext)s",
        url
    ]
    print(f"🔄 Ejecutando: {' '.join(cmd)}")
    
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"❌ Error downloading comments: {e}")
        return None
    
    info_file = f"{video_id}.info.json"
    if os.path.exists(info_file):
        print(f"✅ Metadata file created: {info_file}")
    
    # Look for comments in different possible locations
    possible_files = [
        f"{video_id}.info.json",
        f"{video_id}.comments.json"
    ]
    
    for file in possible_files:
        if os.path.exists(file):
            with open(file, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            # Check if comments are in the data
            if "comments" in data and data["comments"]:
                print(f"✅ Found {len(data['comments'])} comments in {file}")
                return data
            elif isinstance(data, list):
                print(f"✅ Found {len(data)} comments in {file}")
                return {"comments": data, "id": video_id}
    
    print(f"❌ No comments found for video {video_id}")
    return None

def process_video_comments(video_identifier):
    vid = extract_video_id(video_identifier)
    data = download_comments(vid)
    if data:
        data["id"] = vid
    return data

def moderate_comment(comment_text, client):
    return api_manager.moderate_comment_with_client(comment_text, client)

def analyze_comments(video_list, keywords, client):
    results = []
    
    print(f"🔄 Starting comment analysis for {len(video_list)} videos...")
    
    for idx, vid in enumerate(video_list, 1):
        extracted_id = extract_video_id(vid)
        print(f"\n📹 Processing video {idx}/{len(video_list)}: {extracted_id}")
        
        ensure_video_metadata(extracted_id)
        
        data = process_video_comments(vid)
        
        if not data:
            print(f"⚠️ No comments data for video {extracted_id}")
            continue
        
        vid_id = data.get("id", extracted_id)
        comments = data.get("comments", [])
        
        print(f"💬 Found {len(comments)} comments")
        
        if keywords:
            filtered_comments = []
            for comment in comments:
                text = comment.get("text", "")
                if any(kw.lower() in text.lower() for kw in keywords):
                    filtered_comments.append(comment)
            comments = filtered_comments
            print(f"🔍 Filtered to {len(comments)} comments containing keywords: {', '.join(keywords)}")
        
        flagged_count = 0
        for comment_idx, comment in enumerate(comments, 1):
            if comment_idx % 50 == 0:
                print(f"   📊 Processed {comment_idx}/{len(comments)} comments...")
            
            text = comment.get("text", "")
            hate_categories = moderate_comment(text, client)
            if hate_categories:
                flagged_count += 1
                results.append({
                    "Filename": f"{vid_id}.comments",
                    "Timestamp": None,
                    "Texto": text,
                    "Categorías": ", ".join(hate_categories),
                    "YouTubeURL": f"https://www.youtube.com/watch?v={vid_id}&lc={comment.get('id', '')}",
                    "CommentAuthor": comment.get("author", ""),
                    "CommentID": comment.get("id", ""),
                    "AuthorThumbnail": comment.get("author_thumbnail", "")
                })
        
        print(f"🚩 Found {flagged_count} flagged comments for video {extracted_id}")
    
    print(f"\n✅ Comment analysis complete! Total flagged comments: {len(results)}")
    api_manager.print_stats()
    
    return results

def ensure_video_metadata(video_id):
    info_file = f"{video_id}.info.json"
    if not os.path.exists(info_file):
        print(f"📥 Downloading metadata for video: {video_id}")
        url = f"https://www.youtube.com/watch?v={video_id}"
        
        cmd = [
            "yt-dlp",
            "--write-info-json",
            "--skip-download",
            "-o", "%(id)s.%(ext)s",
            url
        ]
        
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=60)
            if os.path.exists(info_file):
                print(f"✅ Metadata downloaded successfully: {info_file}")
        except Exception as e:
            print(f"❌ Failed to download metadata for {video_id}: {e}")

def download_subtitles_for_video(video_url, language):
    print(f"\n🎬 Downloading subtitles for video: {video_url}")
    video_id = extract_video_id(video_url)
    
    language_fallbacks = [language, 'en', 'es', 'auto']
    if language not in language_fallbacks:
        language_fallbacks.insert(0, language)
    
    subtitle_downloaded = False
    
    for lang in language_fallbacks:
        print(f"📝 Trying language: '{lang}'")
        
        cmd = [
            "yt-dlp", 
            "--skip-download",
            "--write-info-json",
            "--sub-lang", lang,
            "--write-auto-subs", 
            "--convert-subs", "srt",
            "-o", "%(id)s.%(ext)s",
            video_url
        ]
        
        try:
            result = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=120)
            
            possible_files = [
                f"{video_id}.{lang}.srt", 
                f"{video_id}.en.srt", 
                f"{video_id}.srt"
            ]
            
            for srt_file in possible_files:
                if os.path.exists(srt_file):
                    print(f"✅ Subtitle file found: {srt_file}")
                    subtitle_downloaded = True
                    break
            
            if subtitle_downloaded:
                break
                
        except Exception as e:
            print(f"❌ Failed with language '{lang}': {e}")
    
    ensure_video_metadata(video_id)
    
    if not subtitle_downloaded:
        print(f"\n❌ Could not download subtitles for video {video_id}")
    
    return subtitle_downloaded

def time_to_seconds(timestamp):
    h, m, s_ms = timestamp.split(":")
    s, ms = s_ms.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0

def clean_text(text):
    timestamp_pattern = re.compile(r'\d{2}:\d{2}:\d{2},\d{3}\s*-->\s*\d{2}:\d{2}:\d{2},\d{3}')
    cleaned_lines = []
    for line in text.splitlines():
        if not timestamp_pattern.search(line):
            cleaned_lines.append(line)
    return " ".join(cleaned_lines).strip()

def parse_srt(srt_text):
    pattern = re.compile(
        r"(\d+)\s*\n"
        r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*"
        r"(\d{2}:\d{2}:\d{2},\d{3})\s*\n"
        r"(.*?)(?=\n\s*\n|\Z)",
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
    max_overlap = 0
    max_possible = min(len(a), len(b))
    for i in range(1, max_possible + 1):
        if a[-i:] == b[:i]:
            max_overlap = i
    return max_overlap

def merge_texts(texts):
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
    blocks = parse_srt(srt_text)
    grouped = group_blocks(blocks, threshold)
    merged_groups = []
    for group_start, blocks_in_group in grouped:
        texts = [text for (_, _, text) in sorted(blocks_in_group, key=lambda x: x[1])]
        merged_text = merge_texts(texts)
        merged_groups.append((int(group_start), merged_text))
    return merged_groups

def convert_srt_file(srt_file, threshold=30):
    with open(srt_file, "r", encoding="utf-8") as f:
        srt_text = f.read()
    merged_groups = process_srt(srt_text, threshold)
    output_lines = []
    for timestamp, text in merged_groups:
        output_lines.append(str(timestamp))
        output_lines.append(text)
        output_lines.append("")
    return "\n".join(output_lines)

def get_video_list(channel_url):
    playlist_url = channel_url.rstrip("/") + "/videos"
    print("🔍 Retrieving video list from:", playlist_url)
    result = subprocess.run(["yt-dlp", "-J", "--flat-playlist", playlist_url],
                            capture_output=True, text=True, check=True)
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
    
    print(f"✅ Found {len(video_list)} videos in channel")
    return video_list

def build_individual_video_command(video_id, base_args):
    """Build a command for processing a single video"""
    # Start with base python command
    cmd = ['python3', 'hatehunter.py']
    
    # Add project
    cmd.extend(['--project', base_args.project])
    
    # Add video URL
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    cmd.extend(['--video', video_url])
    
    # Add language
    cmd.extend(['--language', base_args.language])
    
    # Add keywords if specified
    if base_args.keywords:
        keywords_str = ','.join(base_args.keywords)
        cmd.extend(['--keywords', keywords_str])
    
    # Add threshold
    cmd.extend(['--threshold', str(base_args.threshold)])
    
    # Add rate limit
    cmd.extend(['--rate-limit', str(base_args.rate_limit)])
    
    # Add OpenAI API key if specified
    if base_args.openai_api_key:
        cmd.extend(['--openai-api-key', base_args.openai_api_key])
    
    # Add boolean flags
    if base_args.comments:
        cmd.append('--comments')
    
    if base_args.skip_convert:
        cmd.append('--skip-convert')
    
    if base_args.skip_analyze:
        cmd.append('--skip-analyze')
    
    if base_args.update_ytdlp:
        cmd.append('--update-ytdlp')
    
    if base_args.keep_json:
        cmd.append('--keep-json')
    
    return ' '.join(cmd)

def add_channel_videos_to_queue(channel_url, args):
    """Process channel by adding individual video commands to the queue"""
    print(f"🎯 Channel mode: Processing {channel_url}")
    print("📋 This will add individual video commands to the processing queue")
    
    try:
        # Get list of videos from the channel
        video_list = get_video_list(channel_url)
        
        if not video_list:
            print("❌ No videos found in the channel")
            return
        
        print(f"📹 Found {len(video_list)} videos in channel")
        print(f"🔄 Building individual commands for each video...")
        
        # Build commands for each video
        commands = []
        
        for i, video in enumerate(video_list, 1):
            video_id = video["id"]
            video_title = video.get("title", "Unknown Title")
            
            # Build command for this individual video
            command = build_individual_video_command(video_id, args)
            commands.append(command)
            
            if i % 10 == 0:
                print(f"   📝 Prepared {i}/{len(video_list)} commands...")
        
        print(f"✅ Prepared {len(commands)} video commands")
        
        # Write commands to queue file
        print(f"📤 Adding commands to queue file: {QUEUE_FILE}")
        
        with open(QUEUE_FILE, 'a', encoding='utf-8') as f:
            for command in commands:
                f.write(command + '\n')
        
        print(f"🎉 Successfully added {len(commands)} video commands to the processing queue!")
        print(f"📊 Queue file: {QUEUE_FILE}")
        print(f"🚀 Videos will be processed automatically by the server queue manager")
        print(f"🌐 Monitor progress at: http://localhost:1337/project/{args.project}/videos")
        
        # Create video placeholders in database with 'queued' status
        create_queued_video_placeholders(video_list, args.project)
        
        # Notify the server about new items in queue (if server is running)
        try_notify_server(args.project, len(commands))
        
    except Exception as e:
        print(f"❌ Error processing channel: {e}")
        raise

def create_queued_video_placeholders(video_list, project_name):
    """Create video entries in database with 'queued' status"""
    print(f"🔄 Creating video placeholders in database...")
    
    session = db.get_session()
    try:
        # Get or create project
        project = session.query(Project).filter_by(name=project_name).first()
        if not project:
            project = Project(name=project_name)
            session.add(project)
            session.flush()
        
        created_count = 0
        
        for video in video_list:
            video_id = video["id"]
            video_title = video.get("title", f"Video {video_id}")
            
            # Check if video already exists
            existing_video = session.query(Video).filter_by(
                project_id=project.id,
                video_id=video_id
            ).first()
            
            if not existing_video:
                # Create new video with 'queued' status
                new_video = Video(
                    project_id=project.id,
                    video_id=video_id,
                    title=video_title,
                    webpage_url=f"https://www.youtube.com/watch?v={video_id}",
                    thumbnail=f"https://img.youtube.com/vi/{video_id}/mqdefault.jpg",
                    processing_status='queued'
                )
                session.add(new_video)
                created_count += 1
            else:
                # Update existing video to 'queued' status
                existing_video.processing_status = 'queued'
                existing_video.processing_error = None
        
        session.commit()
        print(f"✅ Created/updated {created_count} video placeholders with 'queued' status")
        
    except Exception as e:
        session.rollback()
        print(f"❌ Error creating video placeholders: {e}")
    finally:
        session.close()

def try_notify_server(project_name, video_count):
    """Try to notify the server about new queue items"""
    try:
        import requests
        
        # Try to ping the server to see if it's running
        response = requests.get('http://localhost:1337/debug/processes', timeout=2)
        
        if response.status_code == 200:
            print(f"✅ Server is running - queue will be processed automatically")
        else:
            print(f"⚠️ Server responded with status {response.status_code}")
            
    except requests.exceptions.RequestException:
        print(f"⚠️ Could not connect to server at localhost:1337")
        print(f"💡 Make sure to start the server: python server.py")
        print(f"   The queue will be processed when the server starts")

def download_subtitles(video_list, language):
    successful_downloads = 0
    failed_downloads = 0
    
    print(f"\n🎬 Starting subtitle download for {len(video_list)} videos...")
    print(f"📝 Primary language: {language}")
    
    for i, video in enumerate(video_list, 1):
        video_id = video["id"]
        video_title = video.get("title", "Unknown Title")
        
        print(f"\n[{i}/{len(video_list)}] Processing: {video_id}")
        print(f"   Title: {video_title}")
        
        url = f"https://www.youtube.com/watch?v={video_id}"
        
        try:
            success = download_subtitles_for_video(url, language)
            if success:
                successful_downloads += 1
                print(f"✅ [{i}/{len(video_list)}] Successfully downloaded subtitles for {video_id}")
            else:
                failed_downloads += 1
                print(f"⚠️ [{i}/{len(video_list)}] No subtitles available for {video_id}")
        
        except Exception as e:
            failed_downloads += 1
            print(f"❌ [{i}/{len(video_list)}] Unexpected error for {video_id}: {e}")
            continue
    
    print(f"\n📊 Subtitle Download Summary:")
    print(f"   ✅ Successful: {successful_downloads}")
    print(f"   ⚠️ Failed/No subtitles: {failed_downloads}")
    print(f"   📁 Total videos processed: {len(video_list)}")

def moderate_text(text):
    return api_manager.moderate_text(text)

def highlight_text(text, keywords):
    if not keywords:
        return text
    for kw in keywords:
        pattern = re.compile(re.escape(kw), re.IGNORECASE)
        text = pattern.sub(lambda m: '<span style="background-color: yellow;">{}</span>'.format(m.group(0)), text)
    return text

def analyze_file(file_path, keywords):
    print(f"🔍 Analyzing file: {file_path}")
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.readlines()
    
    results = []
    filename = os.path.basename(file_path)
    video_id = filename.split('.')[0]
    
    print(f"   Processing {len(content)} lines from {filename}")
    
    for i, line in enumerate(content):
        if keywords and not any(keyword.lower() in line.lower() for keyword in keywords):
            continue
            
        line_clean = line.strip()
        if not line_clean:
            continue
            
        timestamp = None
        if i > 0:
            prev_line = content[i - 1].strip()
            if prev_line.replace('.', '').isdigit():
                timestamp = float(prev_line)

        moderation_response = moderate_text(line_clean)
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
                "Texto": line_clean,
                "Categorías": ", ".join(categories),
                "YouTubeURL": youtube_url
            })
    
    print(f"   🚩 Found {len(results)} flagged items in {filename}")
    return results

def extract_video_metadata(video_id):
    info_file = f"{video_id}.info.json"
    print(f"🔍 Looking for metadata file: {info_file}")
    
    if not os.path.exists(info_file):
        print(f"❌ Metadata file not found: {info_file}")
        return None
    
    print(f"✅ Found metadata file: {info_file}")
    
    try:
        with open(info_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"❌ Error reading metadata file: {e}")
        return None
    
    print(f"📊 Video title: {data.get('title', 'Unknown')}")
    
    # Format duration
    duration = data.get("duration")
    if duration:
        hours, remainder = divmod(duration, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            duration_str = f"{hours}:{minutes:02d}:{seconds:02d}"
        else:
            duration_str = f"{minutes}:{seconds:02d}"
    else:
        duration_str = None
    
    # Format upload date
    upload_date = data.get("upload_date")
    if upload_date and len(upload_date) == 8:
        upload_date = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}"
    
    # Format view count
    view_count = data.get("view_count")
    if view_count:
        if view_count >= 1000000:
            view_count = f"{view_count/1000000:.1f}M"
        elif view_count >= 1000:
            view_count = f"{view_count/1000:.1f}K"
        else:
            view_count = str(view_count)
    
    # Format like count
    like_count = data.get("like_count")
    if like_count:
        if like_count >= 1000000:
            like_count = f"{like_count/1000000:.1f}M"
        elif like_count >= 1000:
            like_count = f"{like_count/1000:.1f}K"
        else:
            like_count = str(like_count)
    
    # Format comment count
    comment_count = data.get("comment_count")
    if comment_count:
        if comment_count >= 1000000:
            comment_count = f"{comment_count/1000000:.1f}M"
        elif comment_count >= 1000:
            comment_count = f"{comment_count/1000:.1f}K"
        else:
            comment_count = str(comment_count)
    
    metadata = {
        "id": video_id,
        "title": data.get("title", "Untitled Video"),
        "uploader": data.get("uploader") or data.get("channel"),
        "uploader_avatar": data.get("uploader_avatar_url") or data.get("channel_avatar_url"),
        "upload_date": upload_date,
        "retrieval_date": datetime.now().strftime("%Y-%m-%d"),
        "duration": duration_str,
        "view_count": view_count,
        "like_count": like_count,
        "comment_count": comment_count,
        "thumbnail": data.get("thumbnail") or f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg",
        "webpage_url": data.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}",
        "quality": data.get("format_note") or data.get("resolution"),
        "has_captions": bool(data.get("subtitles") or data.get("automatic_captions")),
        "is_live": data.get("is_live", False),
        "flagged_subtitles": 0,
        "flagged_comments": 0
    }
    
    print(f"✅ Successfully extracted metadata for: {video_id}")
    return metadata

def cleanup_temporary_files(video_ids, keep_info_json=True):
    """Clean up temporary files after processing"""
    print("\n🧹 Cleaning up temporary files...")
    
    files_to_clean = []
    
    # Patterns for files to clean
    patterns_to_clean = [
        "*.srt",
        "*.s30", 
        "*.comments.json"
    ]
    
    # If we don't want to keep info.json files
    if not keep_info_json:
        patterns_to_clean.append("*.info.json")
    
    for pattern in patterns_to_clean:
        matching_files = glob.glob(pattern)
        files_to_clean.extend(matching_files)
    
    # Also clean video-specific files if video_ids provided
    if video_ids:
        for video_id in video_ids:
            specific_files = [
                f"{video_id}.live_chat.json",
                f"{video_id}.description",
                f"{video_id}.annotations.xml"
            ]
            
            # Only clean info.json if keep_info_json is False
            if not keep_info_json:
                specific_files.append(f"{video_id}.info.json")
            
            for file_path in specific_files:
                if os.path.exists(file_path):
                    files_to_clean.append(file_path)
    
    # Also clean the videos.json file from channel processing
    if os.path.exists("videos.json"):
        files_to_clean.append("videos.json")
    
    # Remove duplicates
    files_to_clean = list(set(files_to_clean))
    
    cleaned_count = 0
    for file_path in files_to_clean:
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                cleaned_count += 1
                print(f"🗑️ Removed: {file_path}")
        except Exception as e:
            print(f"⚠️ Failed to remove {file_path}: {e}")
    
    if cleaned_count > 0:
        print(f"✅ Cleaned up {cleaned_count} temporary files")
        
        if keep_info_json:
            info_files = glob.glob("*.info.json")
            if info_files:
                print(f"📁 Preserved {len(info_files)} .info.json files for video metadata")
    else:
        print("ℹ️ No temporary files to clean")

def merge_analysis_results(keywords, project_name, comment_results=None):
    s30_files = glob.glob("*.s30")
    subtitle_results = []
    
    print(f"🔍 Found {len(s30_files)} .s30 files to analyze")
    
    for file in s30_files:
        file_results = analyze_file(file, keywords)
        subtitle_results.extend(file_results)
    
    if comment_results is None:
        comment_results = []
    
    # Get database session
    session = db.get_session()
    
    try:
        # Get or create project
        project = session.query(Project).filter_by(name=project_name).first()
        if not project:
            project = Project(name=project_name)
            session.add(project)
            session.flush()
        
        # Process videos first
        video_map = {}  # video_id -> Video object
        all_video_ids = set()
        
        # Extract video IDs from results
        for item in subtitle_results + comment_results:
            video_id = item["Filename"].split('.')[0]
            all_video_ids.add(video_id)
        
        # Create or update videos and mark them as completed
        for video_id in all_video_ids:
            video = session.query(Video).filter_by(
                project_id=project.id,
                video_id=video_id
            ).first()
            
            if not video:
                # Extract metadata if available
                metadata = extract_video_metadata(video_id)
                if metadata:
                    video = Video(
                        project_id=project.id,
                        video_id=video_id,
                        title=metadata.get('title', f'Video {video_id}'),
                        uploader=metadata.get('uploader', ''),
                        uploader_avatar=metadata.get('uploader_avatar', ''),
                        upload_date=metadata.get('upload_date', ''),
                        duration=metadata.get('duration', ''),
                        view_count=metadata.get('view_count', ''),
                        like_count=metadata.get('like_count', ''),
                        comment_count=metadata.get('comment_count', ''),
                        thumbnail=metadata.get('thumbnail', ''),
                        webpage_url=metadata.get('webpage_url', ''),
                        quality=metadata.get('quality', ''),
                        has_captions=metadata.get('has_captions', False),
                        is_live=metadata.get('is_live', False),
                        processing_status='completed',
                        processing_completed_at=datetime.utcnow()
                    )
                else:
                    video = Video(
                        project_id=project.id,
                        video_id=video_id,
                        title=f'Video {video_id}',
                        processing_status='completed',
                        processing_completed_at=datetime.utcnow()
                    )
                session.add(video)
                session.flush()
            else:
                # Update existing video to completed
                video.processing_status = 'completed'
                video.processing_completed_at = datetime.utcnow()
                video.processing_error = None
            
            video_map[video_id] = video
            
            # Ensure thumbnail
            ensure_thumbnail(video_id)
        
        # Save subtitle flags
        for item in subtitle_results:
            video_id = item["Filename"].split('.')[0]
            video = video_map.get(video_id)
            
            if video:
                # Check if already exists
                existing = session.query(SubtitleFlag).filter_by(
                    project_id=project.id,
                    video_id=video.id,
                    timestamp=item.get("Timestamp"),
                    text=item["Texto"]
                ).first()
                
                if not existing:
                    subtitle_flag = SubtitleFlag(
                        project_id=project.id,
                        video_id=video.id,
                        timestamp=item.get("Timestamp"),
                        text=highlight_text(item["Texto"], keywords),
                        categories=item.get("Categorías", ""),
                        youtube_url=item.get("YouTubeURL", "")
                    )
                    session.add(subtitle_flag)
        
        # Save comment flags
        for item in comment_results:
            video_id = item["Filename"].split('.')[0]
            video = video_map.get(video_id)
            
            if video:
                # Check if already exists
                existing = session.query(CommentFlag).filter_by(
                    project_id=project.id,
                    video_id=video.id,
                    comment_id=item.get("CommentID", ""),
                    text=item["Texto"]
                ).first()
                
                if not existing:
                    comment_flag = CommentFlag(
                        project_id=project.id,
                        video_id=video.id,
                        comment_author=item.get("CommentAuthor", ""),
                        comment_id=item.get("CommentID", ""),
                        author_thumbnail=item.get("AuthorThumbnail", ""),
                        text=highlight_text(item["Texto"], keywords),
                        categories=item.get("Categorías", ""),
                        youtube_url=item.get("YouTubeURL", "")
                    )
                    session.add(comment_flag)
        
        # Update video counts
        for video in video_map.values():
            video.flagged_subtitles = session.query(SubtitleFlag).filter_by(
                project_id=project.id,
                video_id=video.id
            ).count()
            video.flagged_comments = session.query(CommentFlag).filter_by(
                project_id=project.id,
                video_id=video.id
            ).count()
        
        # Commit all changes
        session.commit()
        
        print(f"\n📊 Results saved to database for project '{project_name}':")
        print(f"   - {len(subtitle_results)} subtitle flags")
        print(f"   - {len(comment_results)} comment flags")
        print(f"   - {len(video_map)} videos processed and marked as completed")
        
        # Clean up temporary files after saving to database
        cleanup_temporary_files(list(all_video_ids), keep_info_json=True)
        
        # Notify connected clients about the update
        try:
            sio = socketio.Client()
            sio.connect('http://localhost:1337')
            sio.emit('data_updated', {
                'project': project_name,
                'type': 'analysis_complete'
            })
            sio.disconnect()
        except Exception as e:
            print(f"⚠️ Could not notify WebSocket server about updates: {e}")
        
    except Exception as e:
        session.rollback()
        print(f"❌ Error saving to database: {e}")
        raise
    finally:
        session.close()
    
    print(f"\n✅ Analysis complete! View results at: http://localhost:1337/project/{project_name}/videos")

def convert_all_srt_files(threshold):
    srt_files = glob.glob("*.srt")
    if not srt_files:
        print("No .srt files found for conversion.")
        return False
    
    print(f"🔄 Converting {len(srt_files)} SRT files...")
    
    for srt_file in srt_files:
        print(f"Converting {srt_file}...")
        output_text = convert_srt_file(srt_file, threshold)
        base = os.path.splitext(srt_file)[0]
        out_file = base + ".s30"
        with open(out_file, "w", encoding="utf-8") as f:
            f.write(output_text)
        print(f"Converted file saved as {out_file}")
    
    return True

def update_ytdlp():
    try:
        print("🔄 Updating yt-dlp to latest version...")
        result = subprocess.run([sys.executable, "-m", "pip", "install", "-U", "yt-dlp"], 
                              capture_output=True, text=True, check=True)
        print("✅ yt-dlp updated successfully")
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ Failed to update yt-dlp: {e}")
        print("Please try manually: pip install -U yt-dlp")
        return False

def check_videos_already_processed(project, video_urls):
    """Check if videos are already processed in the database"""
    session = db.get_session()
    try:
        project_obj = session.query(Project).filter_by(name=project).first()
        if not project_obj:
            return
        
        for url in video_urls:
            vid = extract_video_id(url)
            existing_video = session.query(Video).filter_by(
                project_id=project_obj.id,
                video_id=vid
            ).first()
            
            if existing_video:
                # Check if it has flags
                has_subtitle_flags = session.query(SubtitleFlag).filter_by(
                    project_id=project_obj.id,
                    video_id=existing_video.id
                ).count() > 0
                
                has_comment_flags = session.query(CommentFlag).filter_by(
                    project_id=project_obj.id,
                    video_id=existing_video.id
                ).count() > 0
                
                if has_subtitle_flags or has_comment_flags:
                    print(f"⚠️ Video {vid} is already processed in project '{project}'.")
                    print(f"   - Subtitle flags: {existing_video.flagged_subtitles}")
                    print(f"   - Comment flags: {existing_video.flagged_comments}")
                    print(f"   Continuing anyway...")
    finally:
        session.close()

def update_video_processing_status(project_name, video_urls, status, error_message=None):
    """Update the processing status of videos in the database"""
    session = db.get_session()
    try:
        project = session.query(Project).filter_by(name=project_name).first()
        if not project:
            return
        
        for video_url in video_urls:
            video_id = extract_video_id(video_url)
            if video_id:
                video = session.query(Video).filter_by(
                    project_id=project.id,
                    video_id=video_id
                ).first()
                
                if video:
                    video.processing_status = status
                    if status == 'processing':
                        video.processing_started_at = datetime.utcnow()
                        video.processing_error = None
                    elif status == 'completed':
                        video.processing_completed_at = datetime.utcnow()
                        video.processing_error = None
                    elif status == 'failed':
                        video.processing_error = error_message
                    
                    print(f"🔄 Updated video {video_id} status to: {status}")
        
        session.commit()
    except Exception as e:
        print(f"❌ Error updating video status: {e}")
        session.rollback()
    finally:
        session.close()

def main():
    parser = argparse.ArgumentParser(description="HateHunter Tool with Queue Support for Channel Processing")
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument("--channel", type=str, help="YouTube channel URL (adds all videos to processing queue)")
    group.add_argument("--video", type=str, nargs="+", help="YouTube video URL(s) to process")
    parser.add_argument("--language", type=str, default="en", help="Language for subtitles (default: en)")
    parser.add_argument("--openai-api-key", type=str, help="OpenAI API key for content moderation")
    parser.add_argument("--threshold", type=int, default=30, help="Time threshold (in seconds) for SRT grouping (default: 30)")
    parser.add_argument("--skip-convert", action="store_true", help="Skip converting SRT files")
    parser.add_argument("--skip-analyze", action="store_true", help="Skip analyzing converted files")
    parser.add_argument("--comments", action="store_true", help="Process video comments in addition to subtitles")
    parser.add_argument("--keywords", "-k", type=parse_keywords, default=[],
                        help="Comma-separated list of keywords to search for and highlight in the text. Example: \"islam, jew, black\"")
    parser.add_argument("--project", "-p", type=str, default="default_project",
                        help="Project name to use for organizing results. New data will be added to existing project.")
    parser.add_argument("--rate-limit", type=int, default=10, 
                        help="Maximum API requests per second (default: 10)")
    parser.add_argument("--update-ytdlp", action="store_true", 
                        help="Update yt-dlp to latest version before processing")
    parser.add_argument("--keep-json", action="store_true",
                        help="Keep JSON files after processing (default: clean up)")
    
    args = parser.parse_args()
    
    # Update yt-dlp if requested
    if args.update_ytdlp:
        if not update_ytdlp():
            print("⚠️ Continue anyway? (y/n): ", end="")
            if input().lower() != 'y':
                sys.exit(1)
    
    # Check if we need to process anything
    if not args.video and not args.channel:
        print("Error: Must provide either --video or --channel.")
        parser.print_help()
        sys.exit(1)
    
    # Handle channel mode - NEW QUEUE IMPLEMENTATION
    if args.channel:
        print(f"🎯 Channel Mode: Adding videos to processing queue")
        print(f"📋 Channel URL: {args.channel}")
        print(f"📁 Project: {args.project}")
        
        try:
            add_channel_videos_to_queue(args.channel, args)
            print(f"\n🎉 Channel processing setup complete!")
            print(f"🚀 All videos have been added to the processing queue")
            print(f"🌐 Monitor progress at: http://localhost:1337/project/{args.project}/videos")
            return
            
        except Exception as e:
            print(f"❌ Error in channel mode: {e}")
            sys.exit(1)
    
    # Handle individual video mode - EXISTING IMPLEMENTATION
    if args.video:
        # Initialize API manager for individual video processing
        global api_manager
        api_manager = ModerationAPIManager(max_requests_per_second=args.rate_limit)
        print(f"🚀 API Manager configured: max {args.rate_limit} requests/second")
        
        # Configure OpenAI API
        if not args.skip_analyze or args.comments:
            env_key = os.environ.get("OPENAI_API_KEY")
            if env_key:
                openai.api_key = env_key
                client = OpenAI(api_key=env_key)
            elif args.openai_api_key:
                openai.api_key = args.openai_api_key
                client = OpenAI(api_key=args.openai_api_key)
            else:
                api_key_input = input("Please enter your OpenAI API key: ")
                openai.api_key = api_key_input
                client = OpenAI(api_key=api_key_input)

        video_list = []
        comment_results = []
        
        check_videos_already_processed(args.project, args.video)
        video_list = args.video
        
        # Mark videos as processing at start
        print("🔄 Marking videos as processing...")
        update_video_processing_status(args.project, args.video, 'processing')
        
        try:
            # Download subtitles if not skipped
            if not args.skip_convert:
                for video_url in args.video:
                    download_subtitles_for_video(video_url, args.language)
            
            # Process comments if requested
            if args.comments:
                print("\n📝 Processing comments...")
                comment_results = analyze_comments(video_list, args.keywords, client)
            
            # Convert SRT files if not skipped
            if not args.skip_convert:
                srt_found = convert_all_srt_files(args.threshold)
                if not srt_found and not args.comments:
                    print("⚠️ No subtitles found to process and --comments not specified.")
                    print("The videos might not have subtitles in the requested language.")
                    print("💡 Try:")
                    print("   - Using --update-ytdlp to update yt-dlp")
                    print("   - Using --language en for English subtitles")
                    print("   - Using --comments to process comments instead")
                    print("   - Check if the videos are publicly accessible")
                    
                    # Mark as completed even if no subtitles
                    print("🔄 Marking videos as completed (no subtitles found)...")
                    update_video_processing_status(args.project, args.video, 'completed')
            
            # Analyze results
            if not args.skip_analyze:
                s30_files = glob.glob("*.s30")
                if s30_files or args.comments:
                    merge_analysis_results(args.keywords, args.project, comment_results)
                else:
                    print("⚠️ No subtitle files to analyze. Use --comments to process comments only.")
                    # Mark as completed if no analysis
                    print("🔄 Marking videos as completed (no analysis needed)...")
                    update_video_processing_status(args.project, args.video, 'completed')
                    
                    # Clean up temporary files even if no analysis
                    video_ids = [extract_video_id(url) for url in args.video]
                    cleanup_temporary_files(video_ids, keep_info_json=not args.keep_json)
            elif args.comments and comment_results:
                merge_analysis_results(args.keywords, args.project, comment_results)
            else:
                # Mark as completed if analysis was skipped
                print("🔄 Marking videos as completed (analysis skipped)...")
                update_video_processing_status(args.project, args.video, 'completed')
                
                # Clean up temporary files
                video_ids = [extract_video_id(url) for url in args.video]
                cleanup_temporary_files(video_ids, keep_info_json=not args.keep_json)
                
        except Exception as e:
            print(f"❌ Error during processing: {e}")
            update_video_processing_status(args.project, args.video, 'failed', str(e))
            raise
    
    print("\n🎯 Processing complete!")
    print(f"📊 View results at: http://localhost:1337/project/{args.project}/videos")
    print("💡 Make sure the web server is running: python server.py")

if __name__ == "__main__":
    main()