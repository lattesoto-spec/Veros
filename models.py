from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class Facility(db.Model):
    __tablename__ = "facilities"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.Text, nullable=False)
    ancc_target = db.Column(db.Float, nullable=False, default=215.0)
    rn_target = db.Column(db.Float, nullable=False, default=44.0)


class Resident(db.Model):
    __tablename__ = "residents"
    id = db.Column(db.Integer, primary_key=True)
    facility_id = db.Column(db.Integer, db.ForeignKey("facilities.id"), nullable=False)
    resident_id = db.Column(db.Text, nullable=False)
    name = db.Column(db.Text, nullable=False)
    ancc_class = db.Column(db.Text)
    admitted_date = db.Column(db.Date)
    discharged_date = db.Column(db.Date)


class Staff(db.Model):
    __tablename__ = "staff"
    id = db.Column(db.Integer, primary_key=True)
    facility_id = db.Column(db.Integer, db.ForeignKey("facilities.id"), nullable=False)
    staff_id = db.Column(db.Text, nullable=False)
    name = db.Column(db.Text, nullable=False)
    role = db.Column(db.Text, nullable=False)


class Shift(db.Model):
    __tablename__ = "shifts"
    id = db.Column(db.Integer, primary_key=True)
    staff_id = db.Column(db.Integer, db.ForeignKey("staff.id"), nullable=False)
    facility_id = db.Column(db.Integer, db.ForeignKey("facilities.id"), nullable=False)
    date = db.Column(db.Date, nullable=False)
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    is_direct_care = db.Column(db.Boolean, nullable=False, default=True)
