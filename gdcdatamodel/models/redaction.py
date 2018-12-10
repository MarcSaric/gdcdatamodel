from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, Text, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import relationship

Base = declarative_base()


class RedactionLog(Base):
    """
    Logs a redaction event, each redacted node will be stored as a RedactionEntry
    """

    __tablename__ = 'redaction_log'

    id = Column(Integer, primary_key=True, nullable=False)

    # who initiated the redaction
    initiated_by = Column(Text, nullable=False, index=True)

    # who rescinded this redaction
    rescinded_by = Column(Text, nullable=True)

    # reasons for redaction
    reason = Column(Text, nullable=False)  # long text
    reason_category = Column(Text, nullable=False, index=True)  # short desc

    project_id = Column(Text, nullable=False, index=True)

    date_created = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text('now()'),
    )

    date_rescinded = Column(
        DateTime(timezone=True),
        nullable=True
    )

    entries = relationship("RedactionEntry", back_populates="redaction_log")  # type: list[RedactionEntry]

    @hybrid_property
    def project(self):
        return self.project_id.split("-")[1]

    @hybrid_property
    def program(self):
        return self.project_id.split("-")[0]

    def rescind_all(self, rescinded_by):
        """Rescinds all entries on this redaction log"""
        self.rescinded_by = rescinded_by
        self.date_rescinded = datetime.now()
        for entry in self.entries:
            entry.rescind(rescinded_by)

    @hybrid_property
    def is_rescinded(self):
        """Checks if all redacted entries in this log has been rescinded
        Returns:
           bool: True if all are rescinded
        """
        for entry in self.entries:
            if not entry.rescinded:
                return False
        return True


class RedactionEntry(Base):
    """
    Logs a redacted node, holds enough information to enable
    """
    __tablename__ = 'redaction_entry'

    node_id = Column(Text, nullable=False, primary_key=True)
    version = Column(Text, index=True)
    file_name = Column(Text)
    node_type = Column(Text, nullable=False, index=True)
    release_number = Column(Text, index=True)

    redaction_id = Column(Integer, ForeignKey("redaction_log.id"), nullable=False, primary_key=True)
    redaction_log = relationship("RedactionLog", back_populates="entries")

    rescinded = Column(Boolean, default=False)

    # who rescinded this redaction
    rescinded_by = Column(Text, nullable=True)

    created_datetime = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text('now()'),
    )

    date_rescinded = Column(
        DateTime(timezone=True),
        nullable=True
    )

    def rescind(self, rescinded_by):
        """Performs a rescind action on an entry"""
        self.rescinded = True
        self.rescinded_by = rescinded_by
        self.date_rescinded = datetime.now()
