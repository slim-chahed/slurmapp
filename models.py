from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.sql import func
from database import Base

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False)
    role = Column(String(20), default="user")  # 'user' or 'admin'

class Reservation(Base):
    __tablename__ = "reservations"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    job_name = Column(String(100), nullable=False)
    cpu = Column(Integer, nullable=False)
    ram = Column(Integer, nullable=False)  # GB
    duration = Column(Integer, nullable=False)  # hours
    script = Column(Text, nullable=False, default="")  # User-supplied code or generated Slurm script
    status = Column(String(20), default="pending")  # pending, approved, running, completed, rejected, failed
    slurm_job_id = Column(String(50), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    # Catalogue / Editor / Terminal fields
    language = Column(String(20), nullable=False, default="python")  # python | java | c
    mode = Column(String(20), nullable=False, default="batch")        # batch | editor | terminal
    code = Column(Text, nullable=True)                               # raw user code
    output = Column(Text, nullable=True)                             # execution output
    session_expires_at = Column(DateTime(timezone=True), nullable=True)
    slurm_allocation = Column(String(100), nullable=True)

class ClusterResources(Base):
    __tablename__ = "cluster_resources"
    id = Column(Integer, primary_key=True, index=True)
    total_cpu = Column(Integer, nullable=False)
    free_cpu = Column(Integer, nullable=False)
    total_ram_gb = Column(Integer, nullable=False)
    free_ram_gb = Column(Integer, nullable=False)
    raw = Column(Text, nullable=True)
    recorded_at = Column(DateTime(timezone=True), server_default=func.now())
