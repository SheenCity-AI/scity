from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, text

from scity.models import Base


def test_upgrade_scripts_build_exactly_the_tables_the_code_expects(database_url):
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
    config = Config("alembic.ini")
    config.attributes["url"] = database_url
    command.upgrade(config, "head")
    with engine.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    Base.metadata.drop_all(engine)
    engine.dispose()
    assert diff == []
