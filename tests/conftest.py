from datetime import date
import os
import sys

import pytest
from flask import Flask

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from carelog.models import Facility, ResidentDay, db


@pytest.fixture()
def app():
    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI="sqlite://",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    db.init_app(app)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def facility(app):
    row = Facility(name="Test Home", ancc_target=215.0, rn_target=44.0)
    db.session.add(row)
    db.session.commit()
    db.session.refresh(row)
    db.session.expunge(row)
    return row


def add_resident_days(facility_id: int, day: date, count: int):
    for i in range(count):
        db.session.add(ResidentDay(
            facility_id=facility_id,
            resident_id=f"R-{day.isoformat()}-{i}",
            resident_name=f"Resident {i}",
            date=day,
            occupied=True,
            service_type="permanent",
        ))
