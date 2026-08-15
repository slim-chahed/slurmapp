#!/usr/bin/env python3
"""Cleanup all ZAP/test reservation data from the database."""
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(BASE_DIR, "..")
sys.path.insert(0, APP_DIR)

from database import SessionLocal
from models import Reservation, EditorRun, ResourceAllocation


def clean_zap_data():
    db = SessionLocal()
    try:
        zap_job_names = {"test", "zap", "request", "tere", "thishouldnotexistandhopefullyitwillnot"}
        query = db.query(Reservation).filter(
            Reservation.job_name.in_(zap_job_names)
        )
        reservations = query.all()
        if not reservations:
            print("No ZAP/test reservations found by job name.")
            return
        reservation_ids = [r.id for r in reservations]
        print(f"Found {len(reservation_ids)} ZAP/test reservations: {reservation_ids}")
        db.query(EditorRun).filter(EditorRun.reservation_id.in_(reservation_ids)).delete(synchronize_session=False)
        db.query(ResourceAllocation).filter(ResourceAllocation.reservation_id.in_(reservation_ids)).delete(synchronize_session=False)
        for reservation in reservations:
            db.delete(reservation)
        db.commit()
        print("ZAP/test data cleanup complete.")
    except Exception as e:
        print(f"Cleanup failed: {e}")
        db.rollback()
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    clean_zap_data()
