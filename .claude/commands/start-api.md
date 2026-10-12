Start the FastAPI backend development server.

Launch the backend API server with hot-reload for development.

Arguments: $ARGUMENTS (optional - "docker" to use Docker, otherwise local)

Steps to execute:

For local development (default):
1. Activate the virtual environment (`source venv/bin/activate` from the repository root)
2. Set environment to development: export APP_ENV=dev
3. Ensure PostgreSQL is running (check with: docker ps | grep postgres)
4. Start uvicorn from the repository root (the package is `backend.app`; running from `backend/` stops
   with `ModuleNotFoundError: No module named 'backend'`):
   uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000
   (`make dev` does the same after starting the database and running the bootstrap.)
5. Report server status and URLs:
   - API: http://localhost:8000
   - Docs: http://localhost:8000/api/v1/docs
   - Health: http://localhost:8000/health

For Docker development (if $ARGUMENTS is "docker"):
1. Check if containers are running: docker-compose ps
2. Start services: docker-compose up -d
3. View logs: docker-compose logs -f api
4. Report container status and URLs

Helpful tips:
- Use Ctrl+C to stop the local server
- Check logs for startup errors
- Verify database connection on startup
- Monitor for hot-reload confirmations when files change

Example usage:
- /start-api → start local development server
- /start-api docker → start with Docker Compose
