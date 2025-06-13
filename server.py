#!/usr/bin/env python3
# IMPORTANT: eventlet.monkey_patch() must be first
import eventlet
eventlet.monkey_patch()

import os
import logging
import subprocess
import threading
import json
import time
import re
import sys
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_from_directory, Response
from flask_socketio import SocketIO
from flask_cors import CORS
from datetime import datetime

# Import after monkey_patch
from websocket_handler import WebSocketHandler
from database import db
from models import Project, Video, SubtitleFlag, CommentFlag, ReportedItem

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
app.config['TEMPLATES_AUTO_RELOAD'] = True

# Enable CORS
CORS(app)

# Initialize SocketIO with eventlet
socketio = SocketIO(
    app, 
    cors_allowed_origins="*",
    async_mode='eventlet',
    logger=False,
    engineio_logger=False
)

# Initialize WebSocket handler
ws_handler = WebSocketHandler(socketio)

# Global dictionary to track running analysis processes
running_analyses = {}

# Queue file path
QUEUE_FILE = "hatehunter.tmp"

class HateHunterQueueManager:
    def __init__(self):
        self.processing_lock = threading.Lock()
        self.is_running = True
        self.check_interval = 2  # Check every 2 seconds
        self.current_processing = None
        
    def start_queue_processor(self):
        """Start the background queue processor"""
        def queue_processor():
            while self.is_running:
                try:
                    self.process_queue_file()
                    time.sleep(self.check_interval)
                except Exception as e:
                    logger.error(f"Error in queue processor: {e}")
                    time.sleep(10)  # Wait longer on error
        
        thread = threading.Thread(target=queue_processor, daemon=True)
        thread.start()
        logger.info("HateHunter queue processor started")
    
    def process_queue_file(self):
        """Process the hatehunter.tmp file"""
        with self.processing_lock:
            try:
                # Check if queue file exists
                if not os.path.exists(QUEUE_FILE):
                    return
                
                # Read all lines from the queue file
                with open(QUEUE_FILE, 'r', encoding='utf-8') as f:
                    lines = [line.strip() for line in f.readlines() if line.strip()]
                
                if not lines:
                    # Empty file, remove it
                    os.remove(QUEUE_FILE)
                    logger.info("Empty queue file removed")
                    return
                
                # If we're already processing something, don't start new
                if self.current_processing:
                    return
                
                # Get the first command
                command = lines[0]
                remaining_lines = lines[1:]
                
                # Extract video ID from command for tracking
                video_id = self.extract_video_id_from_command(command)
                project_name = self.extract_project_from_command(command)
                
                logger.info(f"🚀 Starting queue processing for video: {video_id} in project: {project_name}")
                
                # Update video status to processing
                if video_id and project_name:
                    self.update_video_status(project_name, video_id, 'processing')
                
                # Remove the first line from file (write remaining lines back)
                if remaining_lines:
                    with open(QUEUE_FILE, 'w', encoding='utf-8') as f:
                        f.write('\n'.join(remaining_lines) + '\n')
                else:
                    # No more lines, remove file
                    os.remove(QUEUE_FILE)
                    logger.info("Queue file processed completely and removed")
                
                # Start processing the command
                self.current_processing = video_id
                self.execute_command(command, project_name, video_id)
                
            except Exception as e:
                logger.error(f"Error processing queue file: {e}")
    
    def execute_command(self, command, project_name, video_id):
        """Execute a hatehunter command in a separate thread"""
        def run_command():
            try:
                logger.info(f"🔄 Executing: {command}")
                
                # Split command into arguments
                cmd_args = command.split()
                
                # Run the command
                process = subprocess.Popen(
                    cmd_args,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    universal_newlines=True,
                    bufsize=1,
                    cwd=os.getcwd()
                )
                
                # Monitor progress
                while True:
                    output = process.stdout.readline()
                    if output == '' and process.poll() is not None:
                        break
                    if output:
                        line = output.strip()
                        if line:
                            logger.info(f"Queue-analysis [{video_id}]: {line}")
                
                # Wait for completion
                return_code = process.wait()
                logger.info(f"Queue analysis for {video_id} completed with return code: {return_code}")
                
                # Update video status based on result
                if return_code == 0:
                    self.update_video_status(project_name, video_id, 'completed')
                    logger.info(f"✅ Queue analysis completed successfully for {video_id}")
                    
                    # Notify clients about completion
                    self.notify_analysis_complete(project_name, video_id)
                else:
                    self.update_video_status(project_name, video_id, 'failed', f'Analysis failed with return code {return_code}')
                    logger.error(f"❌ Queue analysis failed for {video_id}")
                
            except Exception as e:
                logger.error(f"Error executing command for {video_id}: {e}")
                self.update_video_status(project_name, video_id, 'failed', str(e))
            
            finally:
                # Mark as no longer processing
                self.current_processing = None
                logger.info(f"Finished processing {video_id}, ready for next item")
        
        # Start command execution in background thread
        command_thread = threading.Thread(target=run_command, daemon=True)
        command_thread.start()
    
    def extract_video_id_from_command(self, command):
        """Extract video ID from hatehunter command"""
        try:
            # Look for --video parameter
            parts = command.split()
            for i, part in enumerate(parts):
                if part == '--video' and i + 1 < len(parts):
                    video_url = parts[i + 1]
                    return self.extract_video_id_from_url(video_url)
            return None
        except Exception as e:
            logger.error(f"Error extracting video ID from command: {e}")
            return None
    
    def extract_project_from_command(self, command):
        """Extract project name from hatehunter command"""
        try:
            # Look for --project parameter
            parts = command.split()
            for i, part in enumerate(parts):
                if part == '--project' and i + 1 < len(parts):
                    return parts[i + 1]
            return None
        except Exception as e:
            logger.error(f"Error extracting project from command: {e}")
            return None
    
    def extract_video_id_from_url(self, url):
        """Extract video ID from YouTube URL"""
        if not url:
            return None
        
        if 'youtube.com' in url or 'youtu.be' in url:
            pattern = r"(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})"
            m = re.search(pattern, url)
            if m:
                return m.group(1)
            
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(url)
            qs = parse_qs(parsed.query)
            if 'v' in qs:
                return qs['v'][0]
            return os.path.basename(parsed.path)
        
        return url
    
    def update_video_status(self, project_name, video_id, status, error_message=None):
        """Update video status in database"""
        try:
            session = db.get_session()
            try:
                project = session.query(Project).filter_by(name=project_name).first()
                if not project:
                    logger.warning(f"Project {project_name} not found")
                    return
                
                video = session.query(Video).filter_by(
                    project_id=project.id,
                    video_id=video_id
                ).first()
                
                if video:
                    old_status = video.processing_status
                    video.processing_status = status
                    
                    if status == 'processing':
                        video.processing_started_at = datetime.utcnow()
                        video.processing_error = None
                    elif status == 'completed':
                        video.processing_completed_at = datetime.utcnow()
                        video.processing_error = None
                    elif status == 'failed':
                        video.processing_error = error_message
                    
                    session.commit()
                    logger.info(f"Updated video {video_id} status: {old_status} -> {status}")
                    
                    # Notify WebSocket clients
                    ws_handler.notify_video_status_changed(
                        project_name,
                        video_id,
                        old_status,
                        status
                    )
                else:
                    logger.warning(f"Video {video_id} not found in project {project_name}")
                
            except Exception as e:
                session.rollback()
                logger.error(f"Error updating video status: {e}")
            finally:
                session.close()
        except Exception as e:
            logger.error(f"Error in update_video_status: {e}")
    
    def notify_analysis_complete(self, project_name, video_id):
        """Notify clients when analysis is complete"""
        try:
            session = db.get_session()
            try:
                project = session.query(Project).filter_by(name=project_name).first()
                if not project:
                    return
                
                video = session.query(Video).filter_by(
                    project_id=project.id,
                    video_id=video_id
                ).first()
                
                if video:
                    # Get flag counts
                    subtitle_count = session.query(SubtitleFlag).filter_by(
                        project_id=project.id,
                        video_id=video.id
                    ).count()
                    
                    comment_count = session.query(CommentFlag).filter_by(
                        project_id=project.id,
                        video_id=video.id
                    ).count()
                    
                    flag_counts = {
                        'flagged_subtitles': subtitle_count,
                        'flagged_comments': comment_count
                    }
                    
                    # Notify about analysis completion
                    ws_handler.notify_video_analysis_complete(
                        project_name,
                        video_id,
                        flag_counts
                    )
            finally:
                session.close()
        except Exception as e:
            logger.error(f"Error notifying analysis complete: {e}")
    
    def add_commands_to_queue(self, commands):
        """Add commands to the queue file"""
        try:
            # Append commands to the queue file
            with open(QUEUE_FILE, 'a', encoding='utf-8') as f:
                for command in commands:
                    f.write(command + '\n')
            
            logger.info(f"Added {len(commands)} commands to queue file")
            
            # Immediately create video cards for queued videos
            self.create_queued_video_cards(commands)
            
        except Exception as e:
            logger.error(f"Error adding commands to queue: {e}")
            raise
    
    def create_queued_video_cards(self, commands):
        """Create video cards in 'queued' status for each command"""
        try:
            for command in commands:
                video_id = self.extract_video_id_from_command(command)
                project_name = self.extract_project_from_command(command)
                
                if video_id and project_name:
                    # Check if video already exists, if not create it
                    self.ensure_video_exists(project_name, video_id)
                    # Update status to queued
                    self.update_video_status(project_name, video_id, 'queued')
        except Exception as e:
            logger.error(f"Error creating queued video cards: {e}")
    
    def ensure_video_exists(self, project_name, video_id):
        """Ensure video exists in database, create if not"""
        try:
            session = db.get_session()
            try:
                # Get or create project
                project = session.query(Project).filter_by(name=project_name).first()
                if not project:
                    project = Project(name=project_name)
                    session.add(project)
                    session.flush()
                
                # Check if video exists
                video = session.query(Video).filter_by(
                    project_id=project.id,
                    video_id=video_id
                ).first()
                
                if not video:
                    # Fetch basic metadata
                    metadata = self.fetch_video_metadata(video_id)
                    
                    # Create video
                    video = Video(
                        project_id=project.id,
                        video_id=video_id,
                        title=metadata['title'],
                        uploader=metadata['uploader'],
                        upload_date=metadata['upload_date'],
                        view_count=metadata['view_count'],
                        comment_count=metadata['comment_count'],
                        duration=metadata['duration'],
                        thumbnail=metadata['thumbnail'],
                        webpage_url=f"https://www.youtube.com/watch?v={video_id}",
                        processing_status='pending'
                    )
                    session.add(video)
                    session.commit()
                    
                    logger.info(f"Created video card for {video_id}")
                    
                    # Notify clients about new video
                    video_data = {
                        'id': video.video_id,
                        'title': video.title,
                        'uploader': video.uploader,
                        'upload_date': video.upload_date,
                        'duration': video.duration,
                        'view_count': video.view_count,
                        'comment_count': video.comment_count,
                        'thumbnail': video.thumbnail,
                        'webpage_url': video.webpage_url,
                        'flagged_subtitles': 0,
                        'flagged_comments': 0,
                        'processing_status': 'pending'
                    }
                    
                    ws_handler.notify_video_added(project_name, video_data)
                
            except Exception as e:
                session.rollback()
                logger.error(f"Error ensuring video exists: {e}")
            finally:
                session.close()
        except Exception as e:
            logger.error(f"Error in ensure_video_exists: {e}")
    
    def fetch_video_metadata(self, video_id):
        """Fetch basic video metadata using yt-dlp"""
        try:
            cmd = [
                "yt-dlp",
                "--write-info-json",
                "--skip-download",
                "--print", "%(title)s|%(uploader)s|%(upload_date)s|%(view_count)s|%(comment_count)s|%(duration)s|%(thumbnail)s",
                f"https://www.youtube.com/watch?v={video_id}"
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            
            if result.returncode == 0 and result.stdout.strip():
                # Parse the output
                output = result.stdout.strip()
                parts = output.split('|')
                
                if len(parts) >= 7:
                    title, uploader, upload_date, view_count, comment_count, duration, thumbnail = parts[:7]
                    
                    # Format upload date
                    if upload_date and len(upload_date) == 8:
                        upload_date = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}"
                    
                    # Format duration
                    if duration and duration.isdigit():
                        duration_sec = int(duration)
                        hours, remainder = divmod(duration_sec, 3600)
                        minutes, seconds = divmod(remainder, 60)
                        if hours:
                            duration = f"{hours}:{minutes:02d}:{seconds:02d}"
                        else:
                            duration = f"{minutes}:{seconds:02d}"
                    
                    # Format counts
                    def format_count(count):
                        if count and count.isdigit():
                            count = int(count)
                            if count >= 1000000:
                                return f"{count/1000000:.1f}M"
                            elif count >= 1000:
                                return f"{count/1000:.1f}K"
                            else:
                                return str(count)
                        return count or ''
                    
                    return {
                        'title': title or f'Video {video_id}',
                        'uploader': uploader or '',
                        'upload_date': upload_date or '',
                        'view_count': format_count(view_count),
                        'comment_count': format_count(comment_count),
                        'duration': duration or '',
                        'thumbnail': thumbnail or f"https://img.youtube.com/vi/{video_id}/mqdefault.jpg"
                    }
            
            # Fallback if parsing fails
            return {
                'title': f'Video {video_id}',
                'uploader': '',
                'upload_date': '',
                'view_count': '',
                'comment_count': '',
                'duration': '',
                'thumbnail': f"https://img.youtube.com/vi/{video_id}/mqdefault.jpg"
            }
            
        except Exception as e:
            logger.error(f"Error fetching metadata for {video_id}: {e}")
            return {
                'title': f'Video {video_id}',
                'uploader': '',
                'upload_date': '',
                'view_count': '',
                'comment_count': '',
                'duration': '',
                'thumbnail': f"https://img.youtube.com/vi/{video_id}/mqdefault.jpg"
            }
    
    def stop(self):
        """Stop the queue processor"""
        self.is_running = False

# Initialize queue manager
queue_manager = HateHunterQueueManager()

def extract_video_id(url_or_id):
    """Extract video ID from YouTube URL or return the ID if already extracted"""
    if not url_or_id:
        return None
    
    if 'youtube.com' in url_or_id or 'youtu.be' in url_or_id:
        pattern = r"(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})"
        m = re.search(pattern, url_or_id)
        if m:
            return m.group(1)
        
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(url_or_id)
        qs = parse_qs(parsed.query)
        if 'v' in qs:
            return qs['v'][0]
        return os.path.basename(parsed.path)
    
    return url_or_id

def escape_html(text):
    """Escape HTML characters"""
    if not text:
        return ''
    
    html_escape_table = {
        "&": "&amp;",
        '"': "&quot;",
        "'": "&#x27;",
        ">": "&gt;",
        "<": "&lt;",
    }
    
    return "".join(html_escape_table.get(c, c) for c in str(text))

def format_categories(categories):
    """Format categories with proper HTML tags"""
    if not categories:
        return 'None'
    
    tags = []
    for cat in categories.split(','):
        cat = cat.strip()
        if not cat:
            continue
            
        class_name = ''
        if 'hate' in cat.lower():
            class_name = 'hate'
        elif 'harassment' in cat.lower():
            class_name = 'harassment'
        elif 'violence' in cat.lower():
            class_name = 'violence'
        
        tags.append(f'<span class="category-tag {class_name}">{escape_html(cat)}</span>')
    
    return ''.join(tags) if tags else 'None'

def generate_html_report(project, reported_videos):
    """Generate the HTML report content with proper video metadata and thumbnails"""
    from datetime import datetime
    
    # Calculate totals
    total_videos = len(reported_videos)
    total_subtitles = sum(len(data['subtitles']) for data in reported_videos.values())
    total_comments = sum(len(data['comments']) for data in reported_videos.values())
    
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>HateHunter Report - {project.name}</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            line-height: 1.6;
            margin: 0;
            padding: 20px;
            background-color: #f8fafc;
            color: #334155;
        }}
        .report-header {{
            background: linear-gradient(135deg, #3b82f6 0%, #1e40af 100%);
            color: white;
            padding: 30px;
            border-radius: 12px;
            margin-bottom: 30px;
            text-align: center;
        }}
        .report-header h1 {{
            margin: 0 0 10px 0;
            font-size: 2.5em;
            font-weight: 700;
        }}
        .report-summary {{
            background: white;
            padding: 25px;
            border-radius: 8px;
            margin-bottom: 30px;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
        }}
        .summary-stats {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin-top: 20px;
        }}
        .stat-box {{
            background: #f1f5f9;
            padding: 20px;
            border-radius: 8px;
            text-align: center;
        }}
        .stat-number {{
            font-size: 2em;
            font-weight: bold;
            color: #3b82f6;
        }}
        .stat-label {{
            color: #64748b;
            font-size: 0.9em;
            margin-top: 5px;
        }}
        
        /* Nueva tabla de resumen de videos */
        .videos-summary {{
            background: white;
            margin-bottom: 30px;
            border-radius: 8px;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
            overflow: hidden;
        }}
        .videos-summary h2 {{
            background: #1e293b;
            color: white;
            padding: 20px;
            margin: 0;
            font-size: 1.5em;
            font-weight: 600;
        }}
        .videos-summary-table {{
            width: 100%;
            border-collapse: collapse;
        }}
        .videos-summary-table th {{
            background: #f8fafc;
            padding: 12px;
            text-align: left;
            font-weight: 600;
            color: #374151;
            border-bottom: 2px solid #e5e7eb;
        }}
        .videos-summary-table td {{
            padding: 12px;
            border-bottom: 1px solid #f3f4f6;
            vertical-align: top;
        }}
        .videos-summary-table tr:hover {{
            background-color: #f9fafb;
        }}
        .video-thumbnail-cell {{
            width: 120px;
            text-align: center;
        }}
        .video-thumbnail-img {{
            width: 100px;
            height: 56px;
            object-fit: cover;
            border-radius: 6px;
            box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
        }}
        .video-info-cell {{
            max-width: 300px;
        }}
        .video-title {{
            font-weight: 600;
            margin-bottom: 5px;
            color: #1e293b;
        }}
        .video-meta {{
            font-size: 0.9em;
            color: #64748b;
        }}
        .flags-count {{
            text-align: center;
            font-weight: 600;
        }}
        .flags-count.subtitles {{
            color: #3b82f6;
        }}
        .flags-count.comments {{
            color: #059669;
        }}
        .flags-count.total {{
            color: #dc2626;
            font-size: 1.1em;
        }}
        
        .video-section {{
            background: white;
            margin-bottom: 30px;
            border-radius: 8px;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
            overflow: hidden;
        }}
        .video-header {{
            background: #1e293b;
            color: white;
            padding: 20px;
        }}
        .video-title {{
            font-size: 1.5em;
            font-weight: 600;
            margin-bottom: 10px;
        }}
        .video-meta {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-top: 15px;
        }}
        .meta-item {{
            display: flex;
            flex-direction: column;
        }}
        .meta-label {{
            font-size: 0.85em;
            opacity: 0.8;
            margin-bottom: 3px;
        }}
        .meta-value {{
            font-weight: 500;
        }}
        .meta-value.unknown {{
            color: #94a3b8;
            font-style: italic;
        }}
        .content-section {{
            padding: 25px;
        }}
        .section-title {{
            font-size: 1.3em;
            font-weight: 600;
            margin-bottom: 15px;
            color: #1e293b;
            border-bottom: 2px solid #e2e8f0;
            padding-bottom: 5px;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            margin-bottom: 25px;
            background: white;
            border-radius: 6px;
            overflow: hidden;
        }}
        th {{
            background: #f8fafc;
            padding: 12px;
            text-align: left;
            font-weight: 600;
            color: #374151;
            border-bottom: 2px solid #e5e7eb;
        }}
        td {{
            padding: 12px;
            border-bottom: 1px solid #f3f4f6;
            vertical-align: top;
        }}
        tr:hover {{
            background-color: #f9fafb;
        }}
        .timestamp {{
            background: #dbeafe;
            color: #1e40af;
            padding: 4px 8px;
            border-radius: 4px;
            font-family: monospace;
            font-size: 0.9em;
        }}
        .category-tag {{
            background: #fef3c7;
            color: #92400e;
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 0.8em;
            margin-right: 4px;
        }}
        .category-tag.hate {{
            background: #fecaca;
            color: #991b1b;
        }}
        .category-tag.harassment {{
            background: #fed7aa;
            color: #c2410c;
        }}
        .category-tag.violence {{
            background: #fca5a5;
            color: #dc2626;
        }}
        .youtube-link {{
            color: #3b82f6;
            text-decoration: none;
            font-weight: 500;
            padding: 6px 12px;
            background: #eff6ff;
            border-radius: 6px;
            display: inline-block;
            transition: all 0.2s;
        }}
        .youtube-link:hover {{
            background: #dbeafe;
            transform: translateY(-1px);
        }}
        .author-info {{
            display: flex;
            align-items: center;
            gap: 10px;
        }}
        .author-thumbnail {{
            width: 32px;
            height: 32px;
            border-radius: 50%;
            object-fit: cover;
        }}
        .no-content {{
            text-align: center;
            color: #64748b;
            font-style: italic;
            padding: 20px;
        }}
        .report-footer {{
            text-align: center;
            margin-top: 40px;
            padding: 20px;
            color: #64748b;
            font-size: 0.9em;
        }}
        @media (max-width: 768px) {{
            body {{ padding: 10px; }}
            .video-meta {{ grid-template-columns: 1fr; }}
            .summary-stats {{ grid-template-columns: 1fr; }}
            table {{ font-size: 0.9em; }}
            th, td {{ padding: 8px; }}
            .videos-summary-table th, 
            .videos-summary-table td {{ padding: 8px; }}
            .video-thumbnail-img {{
                width: 80px;
                height: 45px;
            }}
        }}
    </style>
</head>
<body>
    <div class="report-header">
        <h1>🎯 HateHunter Report</h1>
        <p>Content Moderation Analysis for Project: <strong>{escape_html(project.name)}</strong></p>
        <p>Generated on {datetime.now().strftime('%Y-%m-%d at %H:%M:%S')}</p>
    </div>

    <div class="report-summary">
        <h2>📊 Executive Summary</h2>
        <p>This report contains flagged content from YouTube videos that have been analyzed for hate speech, harassment, and other harmful content using AI-powered moderation.</p>
        
        <div class="summary-stats">
            <div class="stat-box">
                <div class="stat-number">{total_videos}</div>
                <div class="stat-label">Videos with Reported Content</div>
            </div>
            <div class="stat-box">
                <div class="stat-number">{total_subtitles}</div>
                <div class="stat-label">Reported Subtitle Segments</div>
            </div>
            <div class="stat-box">
                <div class="stat-number">{total_comments}</div>
                <div class="stat-label">Reported Comments</div>
            </div>
            <div class="stat-box">
                <div class="stat-number">{total_subtitles + total_comments}</div>
                <div class="stat-label">Total Flagged Items</div>
            </div>
        </div>
    </div>

    <!-- Nueva tabla de resumen de videos con miniaturas -->
    <div class="videos-summary">
        <h2>📹 Videos Summary</h2>
        <table class="videos-summary-table">
            <thead>
                <tr>
                    <th class="video-thumbnail-cell">Thumbnail</th>
                    <th class="video-info-cell">Video Information</th>
                    <th>Subtitle Flags</th>
                    <th>Comment Flags</th>
                    <th>Total Flags</th>
                </tr>
            </thead>
            <tbody>
"""

    # Add videos summary table rows
    for video_id, data in reported_videos.items():
        video = data['video']
        subtitles_count = len(data['subtitles'])
        comments_count = len(data['comments'])
        total_flags = subtitles_count + comments_count
        
        # Prepare metadata with proper fallbacks
        title = escape_html(video.title) if video.title else f'Video {video_id}'
        uploader = escape_html(video.uploader) if video.uploader else 'Unknown Channel'
        upload_date = escape_html(video.upload_date) if video.upload_date else 'Unknown Date'
        duration = escape_html(video.duration) if video.duration else 'Unknown Duration'
        view_count = escape_html(str(video.view_count)) if video.view_count else 'Unknown Views'
        
        # Thumbnail URL - use video thumbnail if available, fallback to YouTube default
        thumbnail_url = escape_html(video.thumbnail) if video.thumbnail else f"https://img.youtube.com/vi/{video_id}/mqdefault.jpg"
        
        html += f"""
                <tr>
                    <td class="video-thumbnail-cell">
                        <img src="{thumbnail_url}" 
                             alt="Video thumbnail" 
                             class="video-thumbnail-img"
                             onerror="this.src='https://img.youtube.com/vi/{video_id}/mqdefault.jpg'">
                    </td>
                    <td class="video-info-cell">
                        <div class="video-title">{title}</div>
                        <div class="video-meta">
                            <div><strong>Channel:</strong> {uploader}</div>
                            <div><strong>ID:</strong> {escape_html(video.video_id)}</div>
                            <div><strong>Date:</strong> {upload_date}</div>
                            <div><strong>Duration:</strong> {duration}</div>
                            <div><strong>Views:</strong> {view_count}</div>
                        </div>
                    </td>
                    <td class="flags-count subtitles">{subtitles_count}</td>
                    <td class="flags-count comments">{comments_count}</td>
                    <td class="flags-count total">{total_flags}</td>
                </tr>
"""

    html += """
            </tbody>
        </table>
    </div>
"""

    # Add each video section (existing code continues...)
    for video_id, data in reported_videos.items():
        video = data['video']
        subtitles = data['subtitles']
        comments = data['comments']
        
        # Prepare metadata with proper fallbacks but show actual data when available
        title = escape_html(video.title) if video.title else f'Video {video_id}'
        uploader = escape_html(video.uploader) if video.uploader else '<span class="unknown">Channel information not available</span>'
        upload_date = escape_html(video.upload_date) if video.upload_date else '<span class="unknown">Upload date not available</span>'
        duration = escape_html(video.duration) if video.duration else '<span class="unknown">Duration not available</span>'
        view_count = escape_html(str(video.view_count)) if video.view_count else '<span class="unknown">View count not available</span>'
        comment_count = escape_html(str(video.comment_count)) if video.comment_count else '<span class="unknown">Comment count not available</span>'
        
        # Video metadata section
        html += f"""
    <div class="video-section">
        <div class="video-header">
            <div style="display: flex; align-items: flex-start; gap: 20px;">
                <img src="{escape_html(video.thumbnail) if video.thumbnail else f'https://img.youtube.com/vi/{video_id}/mqdefault.jpg'}" 
                     alt="Video thumbnail" 
                     style="width: 160px; height: 90px; object-fit: cover; border-radius: 8px; box-shadow: 0 4px 8px rgba(0, 0, 0, 0.3); flex-shrink: 0;"
                     onerror="this.src='https://img.youtube.com/vi/{video_id}/mqdefault.jpg'">
                <div style="flex: 1;">
                    <div class="video-title" style="color: white;">🎬 {title}</div>
                    <div class="video-meta" style="color: white;">
                        <div class="meta-item">
                            <div class="meta-label">Video ID</div>
                            <div class="meta-value">{escape_html(video.video_id)}</div>
                        </div>
                        <div class="meta-item">
                            <div class="meta-label">Channel</div>
                            <div class="meta-value">{uploader}</div>
                        </div>
                        <div class="meta-item">
                            <div class="meta-label">Upload Date</div>
                            <div class="meta-value">{upload_date}</div>
                        </div>
                        <div class="meta-item">
                            <div class="meta-label">Duration</div>
                            <div class="meta-value">{duration}</div>
                        </div>
                        <div class="meta-item">
                            <div class="meta-label">Views</div>
                            <div class="meta-value">{view_count}</div>
                        </div>
                        <div class="meta-item">
                            <div class="meta-label">Comments</div>
                            <div class="meta-value">{comment_count}</div>
                        </div>
                        <div class="meta-item">
                            <div class="meta-label">Watch Video</div>
                            <div class="meta-value">
                                <a href="{escape_html(video.webpage_url or f'https://www.youtube.com/watch?v={video.video_id}')}" 
                                   target="_blank" class="youtube-link">▶️ View on YouTube</a>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        
        <div class="content-section">
"""

        # Reported subtitles section
        if subtitles:
            html += f"""
                    <h3 class="section-title">📄 Reported Subtitle Segments ({len(subtitles)})</h3>
                    <table>
                        <thead>
                            <tr>
                                <th style="width: 100px;">Timestamp</th>
                                <th style="width: 50%;">Subtitle Text</th>
                                <th style="width: 200px;">Categories</th>
                                <th style="width: 120px;">Watch at Time</th>
                            </tr>
                        </thead>
                        <tbody>
        """
            for subtitle in subtitles:
                timestamp_display = f'<span class="timestamp">{int(subtitle.timestamp)}s</span>' if subtitle.timestamp else 'N/A'
                categories_html = format_categories(subtitle.categories) if subtitle.categories else 'None'
                youtube_link = f'<a href="{escape_html(subtitle.youtube_url)}" target="_blank" class="youtube-link">▶️ Watch</a>' if subtitle.youtube_url else 'N/A'
                
                # Don't escape HTML in subtitle text - it may contain highlighting spans
                subtitle_text = subtitle.text if subtitle.text else ""
                
                html += f"""
                            <tr>
                                <td>{timestamp_display}</td>
                                <td>{subtitle_text}</td>
                                <td>{categories_html}</td>
                                <td>{youtube_link}</td>
                            </tr>
        """
            html += """
                        </tbody>
                    </table>
        """

        # Reported comments section
        if comments:
            html += f"""
                    <h3 class="section-title">💬 Reported Comments ({len(comments)})</h3>
                    <table>
                        <thead>
                            <tr>
                                <th style="width: 200px;">Author</th>
                                <th style="width: 50%;">Comment Text</th>
                                <th style="width: 200px;">Categories</th>
                                <th style="width: 120px;">View Comment</th>
                            </tr>
                        </thead>
                        <tbody>
        """
            for comment in comments:
                author_html = f'<div class="author-info">'
                if comment.author_thumbnail:
                    author_html += f'<img src="{escape_html(comment.author_thumbnail)}" alt="Author" class="author-thumbnail">'
                author_html += f'<span>{escape_html(comment.comment_author or "Anonymous")}</span></div>'
                
                categories_html = format_categories(comment.categories) if comment.categories else 'None'
                youtube_link = f'<a href="{escape_html(comment.youtube_url)}" target="_blank" class="youtube-link">▶️ View</a>' if comment.youtube_url else 'N/A'
                
                # Don't escape HTML in comment text - it may contain highlighting spans
                comment_text = comment.text if comment.text else ""
                
                html += f"""
                            <tr>
                                <td>{author_html}</td>
                                <td>{comment_text}</td>
                                <td>{categories_html}</td>
                                <td>{youtube_link}</td>
                            </tr>
        """
            html += """
                        </tbody>
                    </table>
        """
        
        # If no reported content (shouldn't happen, but safety check)
        if not subtitles and not comments:
            html += '<div class="no-content">No reported content found for this video.</div>'
        
        html += """
        </div>
    </div>
"""

    # Add footer
    html += f"""
    <div class="report-footer">
        <p><strong>Report Details:</strong></p>
        <p>Project: {escape_html(project.name)} | Generated by HateHunter v1.0</p>
        <p>This report contains content that has been flagged by AI moderation systems and manually marked for review.</p>
        <p>Generated on {datetime.now().strftime('%Y-%m-%d at %H:%M:%S UTC')}</p>
    </div>
</body>
</html>"""

    return html

@app.route('/api/project/<project_name>/report', methods=['POST'])
def generate_report(project_name):
    """Generate HTML report for reported items in a project"""
    session = db.get_session()
    try:
        # Find the project
        project = session.query(Project).filter_by(name=project_name).first()
        if not project:
            return jsonify({'error': 'Project not found'}), 404

        # Get reported items
        reported_items = session.query(ReportedItem).filter_by(
            project_id=project.id
        ).all()

        if not reported_items:
            return jsonify({'error': 'No reported items found for this project. Please mark some subtitles or comments as reported first.'}), 404

        # Group reported items by video
        reported_videos = {}
        
        # Get all reported subtitles and comments with their video info
        for report in reported_items:
            if report.item_type == 'subtitle':
                subtitle = session.query(SubtitleFlag).filter_by(id=report.item_id).first()
                if subtitle and subtitle.video:
                    video_id = subtitle.video.video_id
                    if video_id not in reported_videos:
                        reported_videos[video_id] = {
                            'video': subtitle.video,
                            'subtitles': [],
                            'comments': []
                        }
                    reported_videos[video_id]['subtitles'].append(subtitle)
            
            elif report.item_type == 'comment':
                comment = session.query(CommentFlag).filter_by(id=report.item_id).first()
                if comment and comment.video:
                    video_id = comment.video.video_id
                    if video_id not in reported_videos:
                        reported_videos[video_id] = {
                            'video': comment.video,
                            'subtitles': [],
                            'comments': []
                        }
                    reported_videos[video_id]['comments'].append(comment)

        if not reported_videos:
            return jsonify({'error': 'No valid reported content found'}), 404

        # Generate HTML report
        html_content = generate_html_report(project, reported_videos)
        
        # Return as downloadable HTML file
        response = Response(
            html_content,
            mimetype='text/html',
            headers={
                'Content-Disposition': f'attachment; filename={project_name}_report.html'
            }
        )
        
        logger.info(f"Generated report for project {project_name} with {len(reported_videos)} videos")
        return response

    except Exception as e:
        logger.error(f"Error generating report for project {project_name}: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()

# WebSocket event handlers
@socketio.on('data_updated')
def handle_data_updated(data):
    project_name = data.get('project')
    update_type = data.get('type')
    if project_name:
        ws_handler.notify_data_update(project_name, update_type)

# Routes
@app.route('/')
def index():
    """Main dashboard"""
    return render_template('index.html.j2')

@app.route('/project/<project_name>/videos')
def videos(project_name):
    """Videos view for a project"""
    return render_template('videos.html.j2', project=project_name)

@app.route('/project/<project_name>/subtitles')
def subtitles(project_name):
    """Subtitles view for a project"""
    return render_template('subtitles.html.j2', project=project_name)

@app.route('/project/<project_name>/comments')
def comments(project_name):
    """Comments view for a project"""
    return render_template('comments.html.j2', project=project_name)

@app.route('/static/<path:path>')
def send_static(path):
    """Serve static files"""
    return send_from_directory('static', path)

@app.route('/thumbnails/<path:path>')
def send_thumbnail(path):
    """Serve thumbnail images"""
    return send_from_directory('thumbnails', path)

# API Routes
@app.route('/api/projects', methods=['GET'])
def get_projects():
    """Get all projects"""
    session = db.get_session()
    try:
        projects = session.query(Project).all()
        projects_data = []
        
        for project in projects:
            # Count elements
            subtitle_count = session.query(SubtitleFlag).filter_by(project_id=project.id).count()
            comment_count = session.query(CommentFlag).filter_by(project_id=project.id).count()
            video_count = session.query(Video).filter_by(project_id=project.id).count()
            
            # Get unique categories
            categories = set()
            subtitles = session.query(SubtitleFlag).filter_by(project_id=project.id).all()
            comments = session.query(CommentFlag).filter_by(project_id=project.id).all()
            
            for subtitle in subtitles:
                if subtitle.categories:
                    categories.update(cat.strip() for cat in subtitle.categories.split(','))
            
            for comment in comments:
                if comment.categories:
                    categories.update(cat.strip() for cat in comment.categories.split(','))
            
            projects_data.append({
                'name': project.name,
                'subtitles_count': subtitle_count,
                'comments_count': comment_count,
                'videos_count': video_count,
                'categories': sorted(list(categories)),
                'date': project.updated_at.strftime("%Y-%m-%d %H:%M") if project.updated_at else ""
            })
        
        return jsonify({
            'projects': projects_data
        })
    except Exception as e:
        logger.error(f"Error getting projects: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()

@app.route('/api/project/<project_name>/videos', methods=['GET'])
def get_project_videos(project_name):
    """Get videos for a project"""
    session = db.get_session()
    try:
        project = session.query(Project).filter_by(name=project_name).first()
        if not project:
            return jsonify({'error': 'Project not found'}), 404
        
        videos = session.query(Video).filter_by(project_id=project.id).all()
        videos_data = []
        
        for video in videos:
            # Calculate counters dynamically
            subtitle_count = session.query(SubtitleFlag).filter_by(
                project_id=project.id,
                video_id=video.id
            ).count()
            
            comment_count = session.query(CommentFlag).filter_by(
                project_id=project.id,
                video_id=video.id
            ).count()
            
            videos_data.append({
                'id': video.video_id,
                'title': video.title or f'Video {video.video_id}',
                'uploader': video.uploader or '',
                'upload_date': video.upload_date or '',
                'duration': video.duration or '',
                'view_count': video.view_count or '',
                'like_count': video.like_count or '',
                'comment_count': video.comment_count or '',
                'thumbnail': video.thumbnail or f"https://img.youtube.com/vi/{video.video_id}/mqdefault.jpg",
                'webpage_url': video.webpage_url or f"https://www.youtube.com/watch?v={video.video_id}",
                'flagged_subtitles': subtitle_count,
                'flagged_comments': comment_count,
                'processing_status': video.processing_status or 'completed',
                'processing_error': video.processing_error
            })
        
        return jsonify({
            'project': project_name,
            'videos': videos_data
        })
    except Exception as e:
        logger.error(f"Error getting project videos: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()

@app.route('/api/project/<project_name>/queue', methods=['POST'])
def add_to_queue(project_name):
    """Add commands to the hatehunter.tmp queue file"""
    try:
        data = request.get_json()
        commands = data.get('commands', [])
        
        if not commands:
            return jsonify({'error': 'No commands provided'}), 400
        
        logger.info(f"Adding {len(commands)} commands to queue for project {project_name}")
        
        # Add commands to queue file
        queue_manager.add_commands_to_queue(commands)
        
        return jsonify({
            'success': True,
            'videos_queued': len(commands),
            'message': f'{len(commands)} video(s) added to processing queue'
        }), 200
        
    except Exception as e:
        logger.error(f"Error adding to queue: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/projects', methods=['POST'])
def create_empty_project():
    """Create a new empty project"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400
        
        project_name = data.get('name')
        if not project_name:
            return jsonify({'error': 'Project name is required'}), 400
        
        # Validate project name
        if not re.match(r'^[a-zA-Z0-9_-]+$', project_name):
            return jsonify({'error': 'Project name can only contain letters, numbers, hyphens, and underscores'}), 400
        
        session = db.get_session()
        try:
            # Check if project already exists
            existing_project = session.query(Project).filter_by(name=project_name).first()
            if existing_project:
                return jsonify({'error': 'Project already exists'}), 409
            
            # Create new project
            project = Project(
                name=project_name,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow()
            )
            session.add(project)
            session.commit()
            
            logger.info(f"Created new empty project: {project_name}")
            
            # Notify WebSocket clients
            ws_handler.socketio.emit('data_updated', {
                'type': 'project_created',
                'project_name': project_name
            })
            
            return jsonify({
                'success': True,
                'project': {
                    'name': project.name,
                    'created_at': project.created_at.isoformat(),
                    'subtitles_count': 0,
                    'comments_count': 0,
                    'videos_count': 0,
                    'categories': [],
                    'date': project.created_at.strftime("%Y-%m-%d %H:%M")
                }
            }), 201
            
        except Exception as e:
            session.rollback()
            logger.error(f"Error creating project: {e}")
            return jsonify({'error': str(e)}), 500
        finally:
            session.close()
            
    except Exception as e:
        logger.error(f"Error in create_empty_project: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/project/<project_name>/video/<video_id>', methods=['DELETE'])
def delete_video(project_name, video_id):
    """Delete a video from the given project"""
    session = db.get_session()
    try:
        # Find the project
        project = session.query(Project).filter_by(name=project_name).first()
        if not project:
            return jsonify({'error': 'Project not found'}), 404

        # Find the Video object by project_id and video_id
        video = session.query(Video).filter_by(
            project_id=project.id,
            video_id=video_id
        ).first()
        if not video:
            return jsonify({'error': 'Video not found'}), 404

        # Delete the Video object (cascade will handle related data)
        session.delete(video)
        session.commit()

        # Notify clients that the video list changed
        ws_handler.socketio.emit('video_deleted', {
            'project': project_name,
            'video_id': video_id
        }, room=f"project_{project_name}")

        return jsonify({'success': True}), 200

    except Exception as e:
        session.rollback()
        logger.error(f"Error deleting video: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()

@app.route('/api/project/<project_name>', methods=['DELETE'])
def delete_project(project_name):
    """Delete a project and all its associated data"""
    session = db.get_session()
    try:
        project = session.query(Project).filter_by(name=project_name).first()
        if not project:
            return jsonify({'error': 'Project not found'}), 404
        
        session.delete(project)  # Cascade will delete all related data
        session.commit()
        
        # Notify WebSocket clients
        ws_handler.socketio.emit('project_deleted', {
            'project_name': project_name
        })
        
        return jsonify({'success': True}), 200
    except Exception as e:
        session.rollback()
        logger.error(f"Error deleting project: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()

@app.route('/api/project/<project_name>/subtitles', methods=['GET'])
def get_project_subtitles(project_name):
    """Get subtitles for a project"""
    session = db.get_session()
    try:
        project = session.query(Project).filter_by(name=project_name).first()
        if not project:
            return jsonify({'error': 'Project not found'}), 404
        
        subtitles = session.query(SubtitleFlag).join(Video).filter(
            SubtitleFlag.project_id == project.id
        ).all()
        
        subtitles_data = []
        for subtitle in subtitles:
            subtitles_data.append({
                'id': subtitle.id,
                'video_id': subtitle.video.video_id,
                'timestamp': subtitle.timestamp,
                'text': subtitle.text,
                'categories': subtitle.categories,
                'youtube_url': subtitle.youtube_url
            })
        
        return jsonify({
            'project': project_name,
            'subtitles': subtitles_data
        })
    except Exception as e:
        logger.error(f"Error getting project subtitles: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()

@app.route('/api/project/<project_name>/comments', methods=['GET'])
def get_project_comments(project_name):
    """Get comments for a project"""
    session = db.get_session()
    try:
        project = session.query(Project).filter_by(name=project_name).first()
        if not project:
            return jsonify({'error': 'Project not found'}), 404
        
        # Get reported comments for this project
        reported_comments = set()
        reports = session.query(ReportedItem).filter_by(
            project_id=project.id,
            item_type='comment'
        ).all()
        
        for report in reports:
            reported_comments.add(report.item_id)
        
        comments = session.query(CommentFlag).join(Video).filter(
            CommentFlag.project_id == project.id
        ).all()
        
        comments_data = []
        for comment in comments:
            is_reported = comment.id in reported_comments
            
            comments_data.append({
                'id': comment.id,
                'video_id': comment.video.video_id,
                'author': comment.comment_author,
                'author_thumbnail': comment.author_thumbnail,
                'text': comment.text,
                'categories': comment.categories,
                'youtube_url': comment.youtube_url,
                'is_reported': is_reported
            })
        
        return jsonify({
            'project': project_name,
            'comments': comments_data
        })
    except Exception as e:
        logger.error(f"Error getting project comments: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()

@app.route('/api/hatehunter/analyze', methods=['POST'])
def analyze_videos():
    """Start HateHunter analysis process (primarily for channel mode)"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400
        
        # Validate required fields
        project_name = data.get('project')
        if not project_name:
            return jsonify({'error': 'Project name is required'}), 400
        
        # Check if analysis is already running for this project
        if project_name in running_analyses:
            return jsonify({'error': 'Analysis already running for this project'}), 409
        
        # Validate that we have either videos or channel
        videos = data.get('videos', [])
        channel = data.get('channel')
        
        if not videos and not channel:
            return jsonify({'error': 'Either videos or channel must be specified'}), 400
        
        # Build hatehunter.py command
        try:
            cmd = build_hatehunter_command(data)
            logger.info(f"Built command: {' '.join(cmd)}")
        except Exception as e:
            logger.error(f"Error building command: {e}")
            return jsonify({'error': f'Invalid command parameters: {str(e)}'}), 400
        
        logger.info(f"Starting HateHunter analysis for project: {project_name}")
        logger.info(f"Command: {' '.join(cmd)}")
        
        # Start analysis in background thread
        analysis_id = f"{project_name}_{int(time.time())}"
        running_analyses[project_name] = analysis_id
        
        def run_analysis():
            try:
                logger.info(f"Starting subprocess for analysis {analysis_id}")
                
                # Run the hatehunter.py process
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    universal_newlines=True,
                    bufsize=1,
                    cwd=os.getcwd()
                )
                
                # Send progress updates via WebSocket
                while True:
                    output = process.stdout.readline()
                    if output == '' and process.poll() is not None:
                        break
                    if output:
                        line = output.strip()
                        if line:
                            # Parse output for progress information
                            progress_data = parse_hatehunter_output(line)
                            
                            # Emit progress to WebSocket clients
                            socketio.emit('analysis_progress', {
                                'project': project_name,
                                'analysis_id': analysis_id,
                                **progress_data
                            }, room=f"project_{project_name}")
                
                # Wait for process to complete
                return_code = process.wait()
                logger.info(f"Analysis process completed with return code: {return_code}")
                
                if return_code == 0:
                    # Analysis completed successfully
                    socketio.emit('analysis_progress', {
                        'project': project_name,
                        'analysis_id': analysis_id,
                        'type': 'complete',
                        'message': 'Analysis completed successfully',
                        'progress': 100
                    }, room=f"project_{project_name}")
                    
                    # Notify data update
                    socketio.emit('data_updated', {
                        'project': project_name,
                        'type': 'analysis_complete'
                    }, room=f"project_{project_name}")
                    
                else:
                    # Analysis failed
                    socketio.emit('analysis_progress', {
                        'project': project_name,
                        'analysis_id': analysis_id,
                        'type': 'error',
                        'message': f'Analysis failed with return code {return_code}',
                        'progress': 0
                    }, room=f"project_{project_name}")
                
            except Exception as e:
                logger.error(f"Error running analysis: {e}")
                
                socketio.emit('analysis_progress', {
                    'project': project_name,
                    'analysis_id': analysis_id,
                    'type': 'error',
                    'message': f'Analysis error: {str(e)}',
                    'progress': 0
                }, room=f"project_{project_name}")
            
            finally:
                # Clean up
                if project_name in running_analyses:
                    del running_analyses[project_name]
                logger.info(f"Analysis thread completed for {project_name}")
        
        # Start analysis thread
        analysis_thread = threading.Thread(target=run_analysis, daemon=True)
        analysis_thread.start()
        
        return jsonify({
            'success': True,
            'message': 'Analysis started',
            'analysis_id': analysis_id,
            'project': project_name
        }), 202
        
    except Exception as e:
        logger.error(f"Error starting analysis: {e}")
        return jsonify({'error': str(e)}), 500

def build_hatehunter_command(data):
    """Build the hatehunter.py command from request data"""
    try:
        cmd = ['python3', 'hatehunter.py']
        
        # Project name
        project_name = data.get('project')
        if not project_name:
            raise ValueError('Project name is required')
        cmd.extend(['--project', project_name])
        
        # Input mode: videos or channel
        videos = data.get('videos', [])
        channel = data.get('channel')
        
        if videos:
            # Multiple videos
            cmd.extend(['--video'] + videos)
        elif channel:
            # Channel mode
            cmd.extend(['--channel', channel])
        else:
            raise ValueError('Either videos or channel must be specified')
        
        # Language
        language = data.get('language', 'en')
        cmd.extend(['--language', language])
        
        # OpenAI API key
        openai_key = data.get('openai_api_key')
        if openai_key:
            cmd.extend(['--openai-api-key', openai_key])
        
        # Keywords
        keywords = data.get('keywords', [])
        if keywords:
            if isinstance(keywords, list):
                keywords_str = ','.join(keywords)
            else:
                keywords_str = str(keywords)
            cmd.extend(['--keywords', keywords_str])
        
        # Threshold
        threshold = data.get('threshold', 30)
        cmd.extend(['--threshold', str(threshold)])
        
        # Rate limit
        rate_limit = data.get('rate_limit', 10)
        cmd.extend(['--rate-limit', str(rate_limit)])
        
        # Analysis options
        if data.get('analyze_comments'):
            cmd.append('--comments')
        
        # Advanced options
        if data.get('update_ytdlp'):
            cmd.append('--update-ytdlp')
        
        if data.get('skip_convert'):
            cmd.append('--skip-convert')
        
        if data.get('skip_analyze'):
            cmd.append('--skip-analyze')
        
        return cmd
        
    except Exception as e:
        logger.error(f"Error building command: {e}")
        raise ValueError(f"Invalid command parameters: {str(e)}")

def parse_hatehunter_output(line):
    """Parse HateHunter output line for progress information"""
    # Default progress data
    progress_data = {
        'type': 'log',
        'message': line,
        'progress': None
    }
    
    # Parse specific patterns for progress updates
    line_lower = line.lower()
    
    # Download progress patterns
    if 'downloading' in line_lower and 'video' in line_lower:
        progress_data['type'] = 'step'
        progress_data['message'] = 'Downloading video metadata'
    elif 'subtitle' in line_lower and 'download' in line_lower:
        progress_data['type'] = 'step'
        progress_data['message'] = 'Downloading subtitles'
    elif 'comment' in line_lower and ('download' in line_lower or 'process' in line_lower):
        progress_data['type'] = 'step'
        progress_data['message'] = 'Processing comments'
    elif 'moderat' in line_lower or 'analyz' in line_lower:
        progress_data['type'] = 'step'
        progress_data['message'] = 'Analyzing content with AI moderation'
    elif 'convert' in line_lower and 'srt' in line_lower:
        progress_data['type'] = 'step'
        progress_data['message'] = 'Converting subtitle files'
    elif 'processing complete' in line_lower or 'analysis complete' in line_lower:
        progress_data['type'] = 'complete'
        progress_data['message'] = 'Analysis completed'
        progress_data['progress'] = 100
    elif '❌' in line or 'error' in line_lower:
        progress_data['type'] = 'error'
        progress_data['message'] = line
    elif '✅' in line or 'success' in line_lower:
        progress_data['type'] = 'progress'
        progress_data['message'] = line
    
    # Extract progress percentage if present
    if '%' in line:
        try:
            import re
            match = re.search(r'(\d+)%', line)
            if match:
                progress_data['progress'] = int(match.group(1))
        except:
            pass
    
    return progress_data

# Debug routes
@app.route('/debug/db')
def debug_db():
    """Debug endpoint to check database contents"""
    session = db.get_session()
    try:
        projects = session.query(Project).all()
        debug_info = {
            'projects_count': len(projects),
            'projects': []
        }
        
        for project in projects:
            videos = session.query(Video).filter_by(project_id=project.id).all()
            subtitles = session.query(SubtitleFlag).filter_by(project_id=project.id).all()
            comments = session.query(CommentFlag).filter_by(project_id=project.id).all()
            
            debug_info['projects'].append({
                'id': project.id,
                'name': project.name,
                'videos_count': len(videos),
                'subtitles_count': len(subtitles),
                'comments_count': len(comments),
                'created_at': project.created_at.isoformat() if project.created_at else None
            })
        
        return jsonify(debug_info)
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()

@app.route('/debug/processes')
def debug_processes():
    """Debug endpoint to check running analyses"""
    return jsonify({
        'running_analyses': running_analyses,
        'server_time': datetime.utcnow().isoformat(),
        'python_path': sys.executable,
        'working_directory': os.getcwd(),
        'hatehunter_exists': os.path.exists('hatehunter.py'),
        'queue_file_exists': os.path.exists(QUEUE_FILE),
        'current_processing': queue_manager.current_processing,
        'queue_manager_running': queue_manager.is_running
    })

@app.route('/debug/queue')
def debug_queue():
    """Debug endpoint to check queue file contents"""
    try:
        if not os.path.exists(QUEUE_FILE):
            return jsonify({
                'queue_file_exists': False,
                'commands': [],
                'current_processing': queue_manager.current_processing
            })
        
        with open(QUEUE_FILE, 'r', encoding='utf-8') as f:
            commands = [line.strip() for line in f.readlines() if line.strip()]
        
        return jsonify({
            'queue_file_exists': True,
            'commands_count': len(commands),
            'commands': commands,
            'current_processing': queue_manager.current_processing,
            'queue_manager_running': queue_manager.is_running
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# WebSocket Events
@socketio.on('connect')
def handle_connect():
    """Handle client connection"""
    session_id = request.sid
    logger.info(f"Client connected: {session_id}")
    ws_handler.handle_connect(session_id)

@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnection"""
    session_id = request.sid
    logger.info(f"Client disconnected: {session_id}")
    ws_handler.handle_disconnect(session_id)

@socketio.on('join_project')
def handle_join_project(data):
    """Handle joining a project room"""
    data['session_id'] = request.sid
    ws_handler.handle_join_project(data)

@socketio.on('leave_project')
def handle_leave_project(data):
    """Handle leaving a project room"""
    data['session_id'] = request.sid
    ws_handler.handle_leave_project(data)

@socketio.on('toggle_report')
def handle_toggle_report(data):
    """Handle toggling report status"""
    data['session_id'] = request.sid
    ws_handler.handle_toggle_report(data)

@socketio.on('clear_reports')
def handle_clear_reports(data):
    """Handle clearing all reports"""
    ws_handler.handle_clear_reports(data)

@socketio.on('request_refresh')
def handle_refresh(data):
    """Handle refresh request"""
    session_id = request.sid
    logger.info(f"Refresh requested by {session_id}: {data}")
    if 'project' in data:
        ws_handler.send_project_data(session_id, data['project'])
    else:
        ws_handler.send_initial_data(session_id)

# Error handlers
@app.errorhandler(404)
def not_found(error):
    return jsonify({'error': 'Not found'}), 404

@app.errorhandler(500)
def internal_error(error):
    logger.error(f"Internal error: {error}")
    return jsonify({'error': 'Internal server error'}), 500

# Cleanup function
def cleanup():
    """Cleanup resources"""
    logger.info("Shutting down server...")
    
    # Stop queue manager
    queue_manager.stop()
    
    # Cancel any running analyses
    for project_name, analysis_id in running_analyses.items():
        logger.info(f"Cancelling analysis for project: {project_name}")
    
    running_analyses.clear()
    
    # Clean up queue file if exists
    try:
        if os.path.exists(QUEUE_FILE):
            os.remove(QUEUE_FILE)
            logger.info("Cleaned up queue file")
    except Exception as e:
        logger.error(f"Error cleaning up queue file: {e}")
    
    db.close()

# Main execution
if __name__ == '__main__':
    try:
        logger.info("Starting HateHunter server on port 1337...")
        print("🚀 Server starting on http://localhost:1337")
        print("📊 Debug endpoint available at: http://localhost:1337/debug/db")
        print("🔄 Queue debug endpoint: http://localhost:1337/debug/queue")
        print("🎯 HateHunter analysis endpoint: http://localhost:1337/api/hatehunter/analyze")
        print("📝 Queue endpoint: http://localhost:1337/api/project/{project}/queue")
        
        # Start the queue processor
        queue_manager.start_queue_processor()
        
        socketio.run(
            app, 
            host='0.0.0.0', 
            port=1337,
            debug=False,
            use_reloader=False
        )
    except KeyboardInterrupt:
        cleanup()
    except Exception as e:
        logger.error(f"Server error: {e}")
        cleanup()
                        