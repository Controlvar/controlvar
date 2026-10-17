# Development Documentation

This directory contains development guidelines, best practices, and technical documentation for Controlvar.

## Files

1. [Development Guidelines](guidelines.md): the Python, lint and pull request rules
2. [Testing Guide](testing-guide.md): running the suites, and the rules a new test follows
3. [Runnable Documentation Examples](doc-examples.md): tagging and enrolling a page's shell examples
4. **Database**
   - [Migrations Guide](database/migrations.md)
   - [Using the Data Layer](database/usage.md)
   - [Backup Guide](database/backup.md)
5. [Dependency Injection](dependency-injection.md): a route's session, caller and permission checks
6. [CI workflows](workflow-explanation.md): the required checks, and running them yourself

## Common Tasks

1. **Database Management**
   - [Creating Migrations](database/migrations.md#creating-new-migrations)
   - [Running Backups](database/backup.md#backup-mechanism)

2. **Development Process**
   - [Pull requests](guidelines.md#pull-requests)
   - [Testing](testing-guide.md)

3. **CI**
   - [Required checks](workflow-explanation.md#required-checks)

4. **Deterministic Backend Test Profile**
   - Start test dependencies with Docker Compose:
     - `docker compose -f docker-compose.test.yml up -d postgres-test redis-test`
   - Run the standardized backend release checks:
     - `./scripts/run-backend-tests.sh release`
   - For full local parity (starts/stops containers automatically):
     - `EP_TEST_PROFILE=compose ./scripts/run-backend-tests.sh all`

## Best Practices

1. **Code Quality**
   - Follow the [Development Guidelines](guidelines.md)
   - Write comprehensive tests
   - Document your code
   - Review before committing

2. **Database**
   - Use migrations for schema changes
   - Follow backup procedures
   - Optimize queries
   - Handle transactions properly

3. **Architecture**
   - Use dependency injection
   - Follow SOLID principles
   - Write modular code
   - Consider scalability

## Need Help?

- Review the [Development Guidelines](guidelines.md)
- Check the [Database Guides](database/)
- See the [Workflow Documentation](workflow-explanation.md)
- Contact the development team for assistance
