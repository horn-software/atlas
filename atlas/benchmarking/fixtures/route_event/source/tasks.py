"""Fixture: Celery tasks."""
from celery import Celery

celery = Celery("tasks")


@celery.task
def send_welcome_email(email: str):
    """Send welcome email to new user."""
    return f"sent to {email}"


@celery.task
def generate_report(user_id: int):
    """Generate a user report."""
    return {"user_id": user_id, "report": "done"}
