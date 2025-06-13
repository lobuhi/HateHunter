import json
import logging
from datetime import datetime
from flask_socketio import emit, join_room, leave_room
from sqlalchemy.orm import joinedload
from database import db
from models import Project, Video, SubtitleFlag, CommentFlag, ReportedItem, ActiveUser, VideoQueue

logger = logging.getLogger(__name__)

class WebSocketHandler:
    def __init__(self, socketio):
        self.socketio = socketio
        self.active_users = {}  # session_id -> user_info
        
    def handle_connect(self, session_id):
        """Handle new WebSocket connection"""
        logger.info(f"Client connected: {session_id}")
        
        # Add to active users
        session = db.get_session()
        try:
            user = session.query(ActiveUser).filter_by(session_id=session_id).first()
            if not user:
                user = ActiveUser(
                    session_id=session_id,
                    username=f"User_{session_id[:8]}"
                )
                session.add(user)
            else:
                user.last_activity = datetime.utcnow()
            session.commit()
            
            # Send initial data
            self.send_initial_data(session_id)
            
            # Notify others
            self.broadcast_user_list()
            
        except Exception as e:
            logger.error(f"Error handling connect for {session_id}: {e}")
            session.rollback()
        finally:
            session.close()
    
    def handle_disconnect(self, session_id):
        """Handle WebSocket disconnection"""
        logger.info(f"Client disconnected: {session_id}")
        
        session = db.get_session()
        try:
            user = session.query(ActiveUser).filter_by(session_id=session_id).first()
            if user:
                session.delete(user)
                session.commit()
            
            # Notify others
            self.broadcast_user_list()
            
        except Exception as e:
            logger.error(f"Error handling disconnect for {session_id}: {e}")
            session.rollback()
        finally:
            session.close()
    
    def handle_join_project(self, data):
        """Handle user joining a project room"""
        session_id = data.get('session_id')
        project_name = data.get('project')
        
        if not session_id or not project_name:
            return
        
        # Join room
        join_room(f"project_{project_name}")
        
        # Update user status
        session = db.get_session()
        try:
            user = session.query(ActiveUser).filter_by(session_id=session_id).first()
            if user:
                user.current_project = project_name
                user.last_activity = datetime.utcnow()
                session.commit()
            
            # Send project data
            self.send_project_data(session_id, project_name)
            
        except Exception as e:
            logger.error(f"Error joining project {project_name} for {session_id}: {e}")
            session.rollback()
        finally:
            session.close()
    
    def handle_leave_project(self, data):
        """Handle user leaving a project room"""
        session_id = data.get('session_id')
        project_name = data.get('project')
        
        if not session_id or not project_name:
            return
        
        leave_room(f"project_{project_name}")
    
    def handle_toggle_report(self, data):
        """Handle report toggle for an item"""
        session_id = data.get('session_id')
        project_name = data.get('project')
        item_type = data.get('item_type')  # 'subtitle' or 'comment'
        item_id = data.get('item_id')
        checked = data.get('checked')
        item_data = data.get('item_data', {})  # Datos completos del item
        
        logger.info(f"Toggle report: {session_id} - {item_type} {item_id} - {checked}")
        
        session = db.get_session()
        try:
            project = session.query(Project).filter_by(name=project_name).first()
            if not project:
                logger.warning(f"Project {project_name} not found")
                return
            
            if checked:
                # Add report - verificar si ya existe (GLOBAL, no por usuario)
                existing = session.query(ReportedItem).filter_by(
                    project_id=project.id,
                    item_type=item_type,
                    item_id=item_id
                    # Removido: reported_by=session_id
                ).first()
                
                if not existing:
                    # Crear nuevo reporte con todos los datos
                    report = ReportedItem(
                        project_id=project.id,
                        item_type=item_type,
                        item_id=item_id,
                        reported_by=session_id,  # Mantener para tracking, pero no filtrar por esto
                        item_data=item_data
                    )
                    session.add(report)
                    logger.info(f"Added GLOBAL report: {item_type} {item_id} by {session_id}")
                else:
                    logger.info(f"GLOBAL report already exists: {item_type} {item_id} (originally by {existing.reported_by})")
            else:
                # Remove report (GLOBAL, no por usuario)
                report = session.query(ReportedItem).filter_by(
                    project_id=project.id,
                    item_type=item_type,
                    item_id=item_id
                    # Removido: reported_by=session_id
                ).first()
                
                if report:
                    session.delete(report)
                    logger.info(f"Removed GLOBAL report: {item_type} {item_id} (originally by {report.reported_by})")
                else:
                    logger.info(f"GLOBAL report not found to remove: {item_type} {item_id}")
            
            session.commit()
            
            # Broadcast update to all in project room
            self.socketio.emit('report_updated', {
                'project': project_name,
                'item_type': item_type,
                'item_id': item_id,
                'checked': checked,
                'reported_by': session_id
            }, room=f"project_{project_name}")
            
        except Exception as e:
            logger.error(f"Error toggling report: {e}")
            session.rollback()
        finally:
            session.close()
    
    def handle_clear_reports(self, data):
        """Handle clearing all reports for a project and type"""
        project_name = data.get('project')
        item_type = data.get('item_type')
        
        logger.info(f"Clear all reports: {project_name} - {item_type}")
        
        session = db.get_session()
        try:
            project = session.query(Project).filter_by(name=project_name).first()
            if not project:
                logger.warning(f"Project {project_name} not found")
                return
            
            # Delete all reports of this type
            deleted_count = session.query(ReportedItem).filter_by(
                project_id=project.id,
                item_type=item_type
            ).delete()
            
            session.commit()
            logger.info(f"Cleared {deleted_count} reports for {project_name} - {item_type}")
            
            # Broadcast update
            self.socketio.emit('reports_cleared', {
                'project': project_name,
                'item_type': item_type
            }, room=f"project_{project_name}")
            
        except Exception as e:
            logger.error(f"Error clearing reports: {e}")
            session.rollback()
        finally:
            session.close()
    
    def send_initial_data(self, session_id):
        """Send initial dashboard data to a client"""
        session = db.get_session()
        try:
            # Obtener proyectos con consultas separadas para mejor rendimiento
            projects = session.query(Project).all()
            
            projects_data = []
            for project in projects:
                # Contar elementos por separado
                subtitles_count = session.query(SubtitleFlag).filter_by(project_id=project.id).count()
                comments_count = session.query(CommentFlag).filter_by(project_id=project.id).count()
                videos_count = session.query(Video).filter_by(project_id=project.id).count()
                
                # Obtener categorías únicas
                categories = set()
                
                # Categorías de subtítulos
                subtitle_flags = session.query(SubtitleFlag.categories).filter_by(project_id=project.id).all()
                for (cat_str,) in subtitle_flags:
                    if cat_str:
                        categories.update(cat.strip() for cat in cat_str.split(',') if cat.strip())
                
                # Categorías de comentarios
                comment_flags = session.query(CommentFlag.categories).filter_by(project_id=project.id).all()
                for (cat_str,) in comment_flags:
                    if cat_str:
                        categories.update(cat.strip() for cat in cat_str.split(',') if cat.strip())
                
                projects_data.append({
                    'name': project.name,
                    'subtitles_count': subtitles_count,
                    'comments_count': comments_count,
                    'videos_count': videos_count,
                    'categories': sorted(list(categories)),
                    'date': project.updated_at.strftime("%Y-%m-%d %H:%M") if project.updated_at else ""
                })
            
            logger.info(f"Sending initial data to {session_id}: {len(projects_data)} projects")
            
            emit('initial_data', {
                'projects': projects_data
            }, to=session_id)
            
        except Exception as e:
            logger.error(f"Error sending initial data to {session_id}: {e}")
            # Enviar datos vacíos en caso de error
            emit('initial_data', {
                'projects': []
            }, to=session_id)
        finally:
            session.close()
    
    def send_project_data(self, session_id, project_name):
        """Send project-specific data to a client"""
        session = db.get_session()
        try:
            project = session.query(Project).filter_by(name=project_name).first()
            
            if not project:
                logger.warning(f"Project {project_name} not found")
                return

            # Get reported items for this project (GLOBAL, no filtrar por session_id)
            reported_subtitles = set()
            reported_comments = set()
            
            # CAMBIO: Quitar el filtro reported_by=session_id para hacer reportes globales
            reports = session.query(ReportedItem).filter_by(
                project_id=project.id
                # Removido: reported_by=session_id
            ).all()
            
            # DEBUG: Logging detallado de reportes
            logger.info(f"🔍 DEBUG: Found {len(reports)} GLOBAL reports in project {project_name}")
            for report in reports:
                logger.info(f"🔍 DEBUG: Report - Type: {report.item_type}, ID: {report.item_id}, By: {report.reported_by}")
            
            for report in reports:
                if report.item_type == 'subtitle':
                    reported_subtitles.add(report.item_id)
                else:
                    reported_comments.add(report.item_id)

            logger.info(f"🔍 DEBUG: GLOBAL reported subtitle IDs: {reported_subtitles}")
            logger.info(f"🔍 DEBUG: GLOBAL reported comment IDs: {reported_comments}")

            # Prepare videos data with calculated counters
            videos = session.query(Video).filter_by(project_id=project.id).all()
            videos_data = []
            for video in videos:
                # Calcular contadores dinámicamente
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
                    'flagged_subtitles': subtitle_count,  # Calculado dinámicamente
                    'flagged_comments': comment_count,    # Calculado dinámicamente
                    'processing_status': video.processing_status or 'completed',  # ← NUEVO CAMPO
                    'processing_error': video.processing_error
                })

            # Prepare subtitles data
            subtitles = session.query(SubtitleFlag).join(Video).filter(
                SubtitleFlag.project_id == project.id
            ).all()
            
            subtitles_data = []
            reported_subtitles_count = 0
            for flag in subtitles:
                is_reported = flag.id in reported_subtitles
                if is_reported:
                    reported_subtitles_count += 1
                    logger.info(f"🔍 DEBUG: Subtitle ID {flag.id} is marked as reported")
                
                subtitles_data.append({
                    'id': flag.id,
                    'video_id': flag.video.video_id,
                    'timestamp': flag.timestamp,
                    'text': flag.text or "",
                    'categories': flag.categories or "",
                    'youtube_url': flag.youtube_url or "",
                    'is_reported': is_reported  # Campo agregado
                })

            logger.info(f"🔍 DEBUG: Sending {len(subtitles_data)} subtitles, {reported_subtitles_count} marked as reported")

            # Prepare comments data
            comments = session.query(CommentFlag).join(Video).filter(
                CommentFlag.project_id == project.id
            ).all()
            
            comments_data = []
            reported_comments_count = 0
            for flag in comments:
                is_reported = flag.id in reported_comments
                if is_reported:
                    reported_comments_count += 1
                    logger.info(f"🔍 DEBUG: Comment ID {flag.id} is marked as reported")
                
                comments_data.append({
                    'id': flag.id,
                    'video_id': flag.video.video_id,
                    'author': flag.comment_author or "",
                    'author_thumbnail': flag.author_thumbnail or "",
                    'text': flag.text or "",
                    'categories': flag.categories or "",
                    'youtube_url': flag.youtube_url or "",
                    'is_reported': is_reported  # Campo agregado
                })
            
            logger.info(f"🔍 DEBUG: Sending {len(comments_data)} comments, {reported_comments_count} marked as reported")
            
            logger.info(f"Sending project data to {session_id}: {project_name} - {len(videos_data)} videos, {len(subtitles_data)} subtitles, {len(comments_data)} comments, {len(reported_subtitles)} reported subs, {len(reported_comments)} reported comments")
            
            emit('project_data', {
                'project': project_name,
                'videos': videos_data,
                'subtitles': subtitles_data,
                'comments': comments_data
            }, to=session_id)
            
        except Exception as e:
            logger.error(f"Error sending project data to {session_id}: {e}")
            # Enviar datos vacíos en caso de error
            emit('project_data', {
                'project': project_name,
                'videos': [],
                'subtitles': [],
                'comments': []
            }, to=session_id)
        finally:
            session.close()
    
    def broadcast_user_list(self):
        """Broadcast updated user list to all clients"""
        session = db.get_session()
        try:
            users = session.query(ActiveUser).all()
            user_list = []
            
            for user in users:
                user_list.append({
                    'session_id': user.session_id,
                    'username': user.username,
                    'current_page': user.current_page,
                    'current_project': user.current_project,
                    'last_activity': user.last_activity.isoformat() if user.last_activity else ""
                })
            
            self.socketio.emit('user_count_update', {
                'users': user_list,
                'count': len(user_list)
            })
            
        except Exception as e:
            logger.error(f"Error broadcasting user list: {e}")
        finally:
            session.close()
    
    def notify_data_update(self, project_name, update_type):
        """Notify clients about data updates"""
        try:
            self.socketio.emit('data_updated', {
                'project': project_name,
                'type': update_type,
                'timestamp': datetime.utcnow().isoformat()
            }, room=f"project_{project_name}")
            
            logger.info(f"Notified data update for project {project_name}: {update_type}")
        except Exception as e:
            logger.error(f"Error notifying data update: {e}")

    def notify_video_added(self, project_name, video_data):
        """Notify clients about a new video being added"""
        try:
            self.socketio.emit('video_added', {
                'project': project_name,
                'video': video_data,
                'timestamp': datetime.utcnow().isoformat()
            }, room=f"project_{project_name}")
            
            logger.info(f"Notified video added for project {project_name}: {video_data.get('id', 'unknown')}")
        except Exception as e:
            logger.error(f"Error notifying video added: {e}")

    def notify_video_status_changed(self, project_name, video_id, old_status, new_status, video_data=None):
        """Notify clients about video status changes"""
        try:
            self.socketio.emit('video_status_changed', {
                'project': project_name,
                'video_id': video_id,
                'old_status': old_status,
                'new_status': new_status,
                'video_data': video_data,
                'timestamp': datetime.utcnow().isoformat()
            }, room=f"project_{project_name}")
            
            logger.info(f"Notified video status change for {video_id}: {old_status} -> {new_status}")
        except Exception as e:
            logger.error(f"Error notifying video status change: {e}")

    def notify_video_analysis_complete(self, project_name, video_id, flag_counts):
        """Notify clients when video analysis is complete with flag counts"""
        try:
            self.socketio.emit('video_analysis_complete', {
                'project': project_name,
                'video_id': video_id,
                'flag_counts': flag_counts,
                'timestamp': datetime.utcnow().isoformat()
            }, room=f"project_{project_name}")
            
            logger.info(f"Notified analysis complete for {video_id}: {flag_counts}")
        except Exception as e:
            logger.error(f"Error notifying analysis complete: {e}")

    def notify_queue_update(self, project_name, queue_info):
        """Notify clients about queue status updates"""
        try:
            self.socketio.emit('queue_updated', {
                'project': project_name,
                'queue_info': queue_info,
                'timestamp': datetime.utcnow().isoformat()
            }, room=f"project_{project_name}")
            
            logger.info(f"Notified queue update for project {project_name}: {queue_info}")
        except Exception as e:
            logger.error(f"Error notifying queue update: {e}")

    def notify_multi_video_queued(self, project_name, video_count, video_ids):
        """Notify clients when multiple videos are automatically queued"""
        try:
            self.socketio.emit('multi_video_queued', {
                'project': project_name,
                'video_count': video_count,
                'video_ids': video_ids,
                'timestamp': datetime.utcnow().isoformat()
            }, room=f"project_{project_name}")
            
            logger.info(f"Notified multi-video queue for project {project_name}: {video_count} videos")
        except Exception as e:
            logger.error(f"Error notifying multi-video queue: {e}")