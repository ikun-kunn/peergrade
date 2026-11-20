"""Start PeerGrade on loopback, optionally prompting for a DeepSeek key."""
import argparse
import getpass
import os
import sys

import uvicorn

from ai_service import DEEPSEEK_API_URL, DEEPSEEK_MODEL


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deepseek', action='store_true', help='Enable the official DeepSeek API for this process only')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    if os.getenv('APP_ENV', '').lower() == 'production':
        parser.error('Use the documented production deployment instead of the local launcher.')
    if args.deepseek:
        key = os.getenv('DEEPSEEK_API_KEY')
        if not key and not sys.stdin.isatty():
            parser.error('Run in an interactive terminal or privately configure DEEPSEEK_API_KEY.')
        key = key or getpass.getpass('DeepSeek API key (hidden; not saved): ')
        if not key.strip():
            parser.error('No API key provided. Run without --deepseek to use manual review.')
        os.environ.update(AI_API_KEY=key.strip(), AI_API_URL=DEEPSEEK_API_URL, AI_MODEL=DEEPSEEK_MODEL)
    from app import app, engine
    from db_migrations import upgrade_database

    upgrade_database(engine)
    uvicorn.run(app, host='127.0.0.1', port=args.port)


if __name__ == '__main__':
    main()
