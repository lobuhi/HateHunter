import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlalchemy.pool import StaticPool
from models import Base
import logging

logger = logging.getLogger(__name__)

class Database:
    def __init__(self, db_path='hatehunter.db'):
        self.db_path = db_path
        self.engine = None
        self.SessionLocal = None
        self._init_db()
    
    def _init_db(self):
        # Create database directory if it doesn't exist
        db_dir = os.path.dirname(self.db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir)
        
        # Create engine with proper settings for concurrent access
        self.engine = create_engine(
            f'sqlite:///{self.db_path}',
            connect_args={
                'check_same_thread': False,
                'timeout': 30
            },
            poolclass=StaticPool,
            echo=False  # Cambiar a True para debug de SQL
        )
        
        # Enable WAL mode for better concurrency
        @event.listens_for(self.engine, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA foreign_keys=ON")  # Habilitar foreign keys
            cursor.close()
        
        # Create session factory
        self.SessionLocal = scoped_session(
            sessionmaker(
                autocommit=False,
                autoflush=False,
                bind=self.engine
            )
        )
        
        # Create tables
        try:
            Base.metadata.create_all(bind=self.engine)
            logger.info(f"Database initialized at {self.db_path}")
            
            # Verificar que las tablas se crearon correctamente
            self._verify_tables()
            
        except Exception as e:
            logger.error(f"Error initializing database: {e}")
            raise
    
    def _verify_tables(self):
        """Verificar que todas las tablas necesarias existen"""
        try:
            session = self.get_session()
            # Intentar una consulta simple para verificar las tablas
            from models import Project, Video, SubtitleFlag, CommentFlag, VideoQueue
            
            # Verificar tabla projects
            session.query(Project).count()
            logger.info("✅ Projects table verified")
            
            # Verificar tabla videos
            session.query(Video).count()
            logger.info("✅ Videos table verified")
            
            # Verificar tabla subtitle_flags
            session.query(SubtitleFlag).count()
            logger.info("✅ SubtitleFlags table verified")
            
            # Verificar tabla comment_flags
            session.query(CommentFlag).count()
            logger.info("✅ CommentFlags table verified")
            
            # Verificar tabla video_queue (nueva)
            try:
                session.query(VideoQueue).count()
                logger.info("✅ VideoQueue table verified")
            except Exception as queue_error:
                logger.warning(f"VideoQueue table verification failed (this is normal for existing databases): {queue_error}")
                # La tabla se creará automáticamente en la siguiente operación
            
            session.close()
            logger.info("Database structure verification completed successfully")
            
        except Exception as e:
            logger.error(f"Database verification failed: {e}")
            # No re-lanzar la excepción aquí, solo log
    
    def get_session(self):
        """Get a database session"""
        try:
            return self.SessionLocal()
        except Exception as e:
            logger.error(f"Error creating database session: {e}")
            raise
    
    def close(self):
        """Close database connections"""
        try:
            if self.SessionLocal:
                self.SessionLocal.remove()
            if self.engine:
                self.engine.dispose()
            logger.info("Database connections closed")
        except Exception as e:
            logger.error(f"Error closing database: {e}")

    def migrate_database(self):
        """Perform database migrations for new features"""
        try:
            logger.info("🔄 Checking for database migrations...")
            
            # Check if VideoQueue table exists
            session = self.get_session()
            try:
                session.query(VideoQueue).count()
                logger.info("✅ VideoQueue table already exists")
            except Exception:
                logger.info("🔧 VideoQueue table doesn't exist, creating...")
                # Create the new table
                from models import VideoQueue
                VideoQueue.__table__.create(self.engine, checkfirst=True)
                logger.info("✅ VideoQueue table created successfully")
            finally:
                session.close()
                
            # Add any future migrations here
            logger.info("✅ Database migrations completed")
            
        except Exception as e:
            logger.error(f"Error during database migration: {e}")
            raise

# Global database instance
db = Database()

# Función de utilidad para debug
def debug_database():
    """Función para debug de la base de datos"""
    session = db.get_session()
    try:
        from models import Project, Video, SubtitleFlag, CommentFlag, VideoQueue
        
        print("\n=== DATABASE DEBUG INFO ===")
        print(f"Database path: {db.db_path}")
        print(f"Database exists: {os.path.exists(db.db_path)}")
        
        if os.path.exists(db.db_path):
            print(f"Database size: {os.path.getsize(db.db_path)} bytes")
        
        # Contar registros
        projects_count = session.query(Project).count()
        videos_count = session.query(Video).count()
        subtitles_count = session.query(SubtitleFlag).count()
        comments_count = session.query(CommentFlag).count()
        
        try:
            queue_count = session.query(VideoQueue).count()
        except Exception:
            queue_count = "N/A (table not created yet)"
        
        print(f"Projects: {projects_count}")
        print(f"Videos: {videos_count}")
        print(f"Subtitle flags: {subtitles_count}")
        print(f"Comment flags: {comments_count}")
        print(f"Queue items: {queue_count}")
        
        # Mostrar proyectos
        projects = session.query(Project).all()
        for project in projects:
            print(f"\nProject: {project.name} (ID: {project.id})")
            project_videos = session.query(Video).filter_by(project_id=project.id).count()
            project_subtitles = session.query(SubtitleFlag).filter_by(project_id=project.id).count()
            project_comments = session.query(CommentFlag).filter_by(project_id=project.id).count()
            
            try:
                project_queue = session.query(VideoQueue).filter_by(project_id=project.id).count()
            except Exception:
                project_queue = "N/A"
                
            print(f"  - Videos: {project_videos}")
            print(f"  - Subtitles: {project_subtitles}")
            print(f"  - Comments: {project_comments}")
            print(f"  - Queue items: {project_queue}")
        
        # Queue status if table exists
        try:
            queue_items = session.query(VideoQueue).all()
            if queue_items:
                print(f"\n=== QUEUE STATUS ===")
                for item in queue_items:
                    print(f"Queue Item {item.id}: Video {item.video.video_id} - Status: {item.status} - Priority: {item.priority}")
        except Exception:
            print("\n=== QUEUE STATUS ===")
            print("VideoQueue table not available")
        
        print("=== END DEBUG INFO ===\n")
        
    except Exception as e:
        print(f"Error in debug: {e}")
    finally:
        session.close()

if __name__ == "__main__":
    # Run migrations on direct execution
    db.migrate_database()
    debug_database()