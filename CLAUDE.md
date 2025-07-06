# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Architecture

This is a microservices-based AI bootcamp application with the following components:

- **Backend (FastAPI)**: Main API server handling authentication, LLM interactions, and file operations
- **Frontend (Flask)**: Web UI with Keycloak authentication integration
- **Analytics Service**: RabbitMQ consumer for processing events and storing analytics in Redis
- **Database**: PostgreSQL for persistent data storage
- **Message Queue**: RabbitMQ for asynchronous event processing
- **Cache**: Redis for session storage and analytics
- **Auth**: Keycloak for OpenID Connect authentication
- **Storage**: MinIO for file storage
- **Proxy**: Nginx for SSL termination and load balancing

## Development Commands

### Starting the Application
```bash
# Start all services
docker-compose up -d

# Start with logs
docker-compose up

# Rebuild specific service
docker-compose up --build <service-name>
```

### Service Management
```bash
# View running containers
docker-compose ps

# View logs for specific service
docker-compose logs -f <service-name>

# Execute commands in running container
docker-compose exec <service-name> bash
```

### Database Operations
```bash
# Access PostgreSQL
docker-compose exec postgres psql -U user -d dbname

# Access PgAdmin (web UI)
# Navigate to http://localhost:8082
# Email: admin@admin.com, Password: admin
```

### Development Environment
- Backend runs on port 8000
- Frontend runs on port 5000
- Keycloak runs on port 8080
- RabbitMQ management UI on port 15672 (guest/guest)
- MinIO console on port 9001 (minio/minio123)
- Nginx serves on ports 80/443

## Key Configuration Files

- `.env`: Environment variables (required for all services)
- `docker-compose.yml`: Service orchestration
- `nginx/nginx.conf`: Reverse proxy configuration
- `nginx/certs/`: SSL certificates

## Backend Architecture (FastAPI)

Located in `/backend/`:
- `main.py`: Main FastAPI application with authentication and LLM endpoints
- `database.py`: SQLAlchemy models and database connection
- `rabbitmq_consumer.py`: Background consumer for message processing
- Uses Gemini API for LLM interactions
- Integrates with Keycloak for JWT token validation
- Publishes events to RabbitMQ for analytics

## Frontend Architecture (Flask)

Located in `/frontend/`:
- `app.py`: Main Flask application with OIDC authentication flow
- `templates/`: Jinja2 templates for UI
- Uses Flask-Session for session management
- Communicates with backend via HTTP requests
- Implements Keycloak authentication callback

## Analytics Service

Located in `/analytics/`:
- `analytics.py`: RabbitMQ consumer processing events from multiple queues
- Stores analytics data in Redis using counters and sorted sets
- Processes: LLM events, file events, and file-AI events
- Provides real-time analytics aggregation

## Database Schema

The application uses PostgreSQL with two main tables:
- `llm_logs`: Stores LLM interaction logs
- `user_logs`: Stores user activity logs

## Authentication Flow

1. User accesses frontend
2. Redirected to Keycloak for authentication
3. Keycloak returns authorization code
4. Frontend exchanges code for access token
5. Token stored in session and used for backend API calls
6. Backend validates JWT tokens against Keycloak

## Message Queue Architecture

RabbitMQ queues:
- `llm-events`: LLM interaction events
- `file-events`: File operation events  
- `file-ai-events`: File AI processing events

Events are published by backend services and consumed by analytics service.

## Common Issues

- Ensure `.env` file is properly configured with all required variables
- Services depend on each other - wait for dependencies to be healthy
- SSL certificates in `nginx/certs/` must be valid for HTTPS
- Keycloak requires proper client configuration for OIDC flow