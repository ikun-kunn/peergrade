import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sqlalchemy import create_engine

from tests.check_restore import run_postgres_tool


class RestoreToolTests(unittest.TestCase):
    def test_client_settings_do_not_include_driver_objects_or_password_arguments(self):
        engine = create_engine('postgresql+psycopg://demo:synthetic-password@localhost:5432/test?options=-csearch_path%3Dtest_schema')
        self.addCleanup(engine.dispose)
        with TemporaryDirectory() as scratch:
            service_path = Path(scratch) / 'service.conf'

            def run(arguments, **options):
                self.assertNotIn('synthetic-password', ' '.join(arguments))
                self.assertEqual(options['env']['PGSERVICEFILE'], str(service_path))
                settings = service_path.read_text(encoding='utf-8')
                self.assertIn('dbname=test\n', settings)
                self.assertIn('password=synthetic-password\n', settings)
                self.assertIn('options=-csearch_path=test_schema\n', settings)
                self.assertNotIn('context=', settings)
                return type('Result', (), {'returncode': 0})()

            with patch('tests.check_restore.subprocess.run', side_effect=run) as client:
                run_postgres_tool('pg_dump', engine, service_path, '--format=custom')
                client.assert_called_once()
            self.assertFalse(service_path.exists())
