from __future__ import annotations

import os

from flask import Flask
from flask_sqlalchemy import SQLAlchemy


db = SQLAlchemy()


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-key"),
        SQLALCHEMY_DATABASE_URI=os.environ.get("DATABASE_URL", "sqlite:///club.db"),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )

    if test_config:
        app.config.update(test_config)

    db.init_app(app)

    with app.app_context():
        from . import models  # noqa: F401
        from .models import ensure_admin_user
        from .services.appointments import run_maintenance
        from .services.settings_store import ensure_default_settings

        db.create_all()
        ensure_default_settings(db.session)
        ensure_admin_user(db.session)
        db.session.commit()
        run_maintenance(db.session)
        db.session.commit()

    from .web import bp as web_bp

    app.register_blueprint(web_bp)
    return app
