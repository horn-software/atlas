"""Fixture: Flask routes and Celery tasks for route/event graph testing."""
import os
from flask import Flask, jsonify

app = Flask(__name__)


@app.route("/api/users", methods=["GET"])
def get_users():
    """Return all users."""
    return jsonify([])


@app.route("/api/users/<int:user_id>", methods=["GET"])
def get_user(user_id: int):
    """Return a single user."""
    return jsonify({"id": user_id})


@app.route("/api/users", methods=["POST"])
def create_user():
    """Create a new user and queue a welcome email."""
    send_welcome_email.delay("new@example.com")
    return jsonify({"created": True}), 201


def _get_db_url():
    return os.getenv("DATABASE_URL")
