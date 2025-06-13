from sqlalchemy import Column, Integer, String, Text, DateTime, Float, Boolean, ForeignKey, JSON
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from datetime import datetime

Base = declarative_base()

class Project(Base):
    __tablename__ = 'projects'
    
    id = Column(Integer, primary_key=True)
    name = Column(String(255), unique=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    videos = relationship('Video', back_populates='project', cascade='all, delete-orphan')
    subtitle_flags = relationship('SubtitleFlag', back_populates='project', cascade='all, delete-orphan')
    comment_flags = relationship('CommentFlag', back_populates='project', cascade='all, delete-orphan')
    reported_items = relationship('ReportedItem', back_populates='project', cascade='all, delete-orphan')
    video_queue = relationship('VideoQueue', back_populates='project', cascade='all, delete-orphan')

class Video(Base):
    __tablename__ = 'videos'
    
    id = Column(Integer, primary_key=True)
    video_id = Column(String(50), nullable=False)
    project_id = Column(Integer, ForeignKey('projects.id'), nullable=False)
    title = Column(String(500))
    uploader = Column(String(255))
    uploader_avatar = Column(String(500))
    upload_date = Column(String(20))
    retrieval_date = Column(DateTime, default=datetime.utcnow)
    duration = Column(String(20))
    view_count = Column(String(20))
    like_count = Column(String(20))
    comment_count = Column(String(20))
    thumbnail = Column(String(500))
    webpage_url = Column(String(500))
    quality = Column(String(50))
    has_captions = Column(Boolean, default=False)
    is_live = Column(Boolean, default=False)
    flagged_subtitles = Column(Integer, default=0)
    flagged_comments = Column(Integer, default=0)
    
    # NUEVO CAMPO: Estado de procesamiento
    processing_status = Column(String(50), default='pending')  # 'pending', 'processing', 'completed', 'failed'
    processing_started_at = Column(DateTime)
    processing_completed_at = Column(DateTime)
    processing_error = Column(Text)  # Para almacenar errores si falla el procesamiento
    
    # Relationships
    project = relationship('Project', back_populates='videos')
    subtitle_flags = relationship('SubtitleFlag', back_populates='video', cascade='all, delete-orphan')
    comment_flags = relationship('CommentFlag', back_populates='video', cascade='all, delete-orphan')
    queue_items = relationship('VideoQueue', back_populates='video', cascade='all, delete-orphan')

class VideoQueue(Base):
    __tablename__ = 'video_queue'
    
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey('projects.id'), nullable=False)
    video_id = Column(Integer, ForeignKey('videos.id'), nullable=False)
    
    # Queue status: 'queued', 'processing', 'completed', 'failed'
    status = Column(String(50), default='queued', nullable=False)
    
    # Analysis parameters stored as JSON
    analysis_params = Column(JSON)  # Stores all analysis parameters for this video
    
    # Priority and ordering
    priority = Column(Integer, default=0)  # Higher number = higher priority
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    
    # Error handling
    error_message = Column(Text)
    retry_count = Column(Integer, default=0)
    max_retries = Column(Integer, default=3)
    
    # Relationships
    project = relationship('Project', back_populates='video_queue')
    video = relationship('Video', back_populates='queue_items')

class SubtitleFlag(Base):
    __tablename__ = 'subtitle_flags'
    
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey('projects.id'), nullable=False)
    video_id = Column(Integer, ForeignKey('videos.id'), nullable=False)
    timestamp = Column(Float)
    text = Column(Text)
    categories = Column(String(500))
    youtube_url = Column(String(500))
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    project = relationship('Project', back_populates='subtitle_flags')
    video = relationship('Video', back_populates='subtitle_flags')

class CommentFlag(Base):
    __tablename__ = 'comment_flags'
    
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey('projects.id'), nullable=False)
    video_id = Column(Integer, ForeignKey('videos.id'), nullable=False)
    comment_author = Column(String(255))
    comment_id = Column(String(100))
    author_thumbnail = Column(String(500))
    text = Column(Text)
    categories = Column(String(500))
    youtube_url = Column(String(500))
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    project = relationship('Project', back_populates='comment_flags')
    video = relationship('Video', back_populates='comment_flags')

class ReportedItem(Base):
    __tablename__ = 'reported_items'
    
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey('projects.id'), nullable=False)
    item_type = Column(String(20))  # 'subtitle' or 'comment'
    item_id = Column(Integer)  # ID from subtitle_flags or comment_flags
    reported_by = Column(String(100))  # Client session ID
    reported_at = Column(DateTime, default=datetime.utcnow)
    item_data = Column(JSON)  # Store full item data for reports
    
    # Relationships
    project = relationship('Project', back_populates='reported_items')

class ActiveUser(Base):
    __tablename__ = 'active_users'
    
    id = Column(Integer, primary_key=True)
    session_id = Column(String(100), unique=True)
    username = Column(String(100))
    connected_at = Column(DateTime, default=datetime.utcnow)
    last_activity = Column(DateTime, default=datetime.utcnow)
    current_page = Column(String(100))
    current_project = Column(String(100))