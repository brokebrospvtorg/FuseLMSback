from sqlalchemy import Column, Text, TIMESTAMP, Integer

from app.core.database import Base


class SystemSettings(Base):
    __tablename__ = "system_settings"

    id = Column(Integer, primary_key=True, default=1)
    license_expiry_date = Column(TIMESTAMP(timezone=True), nullable=False)
    school_name = Column(Text, nullable=False)
    activated_at = Column(TIMESTAMP(timezone=True))
