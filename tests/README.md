# LTS (Live Trading System) - Test Suite

This document describes the comprehensive test suite for the LTS (Live Trading System) project. The test suite is organized into four levels following the test pyramid approach: acceptance, system, integration, and unit tests.

## Test Structure

The test suite follows a behavior-driven approach where all tests are implementation-independent and focus on validating required behaviors rather than implementation details.

```
tests/
├── acceptance/          # End-to-end user scenarios
│   └── test_acceptance.py
├── system/             # System-wide behaviors and workflows
│   └── test_system.py
├── integration/        # Component interactions
│   └── test_integration.py
├── unit/               # Individual component behaviors
│   └── test_unit.py
├── conftest.py         # Shared test configuration and fixtures
├── plan_acceptance.md  # Acceptance test plan
├── plan_system.md      # System test plan  
├── plan_integration.md # Integration test plan
└── plan_unit.md        # Unit test plan
```

## Test Levels

### 1. Acceptance Tests (`tests/acceptance/`)
- **Purpose**: Validate end-to-end user stories and business requirements
- **Coverage**: Complete user workflows from registration to trading
- **Examples**: User registration, portfolio creation, trading execution
- **Test Cases**: 15 test cases covering all user stories
- **Risk Level**: Critical - These tests must pass for production deployment

### 2. System Tests (`tests/system/`)
- **Purpose**: Validate system-wide behaviors and non-functional requirements
- **Coverage**: End-to-end system workflows, performance, security, reliability
- **Examples**: Multi-portfolio execution, plugin lifecycle, database integrity
- **Test Cases**: 14 test cases covering system architecture
- **Risk Level**: High - System functionality and performance validation

### 3. Integration Tests (`tests/integration/`)
- **Purpose**: Validate interactions between modules and components
- **Coverage**: Plugin communication, database integration, API endpoints
- **Examples**: Plugin lifecycle, database transactions, API authentication
- **Test Cases**: 13 test cases covering component interactions
- **Risk Level**: High - Component integration validation

### 4. Unit Tests (`tests/unit/`)
- **Purpose**: Validate individual component behaviors in isolation
- **Coverage**: Individual methods, classes, and functions
- **Examples**: Plugin base classes, authentication methods, validation logic
- **Test Cases**: 20 test cases covering individual components
- **Risk Level**: Medium to High - Component-level validation

## Running Tests

### Prerequisites

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Ensure the app directory is in your Python path (handled automatically by conftest.py)

### Running All Tests

```bash
# Using the test runner (recommended)
python run_tests.py

# Using pytest directly
pytest tests/

# With verbose output
python run_tests.py -v
```

### Running Specific Test Levels

```bash
# Run only acceptance tests
python run_tests.py acceptance

# Run only system tests
python run_tests.py system

# Run only integration tests
python run_tests.py integration

# Run only unit tests
python run_tests.py unit
```

### Running Tests with Coverage

```bash
# Generate coverage report
python run_tests.py --coverage

# Coverage report will be in htmlcov/index.html
```

### Running Tests in Parallel

```bash
# Run tests in parallel for faster execution
python run_tests.py --parallel
```

### Running Specific Test Categories

```bash
# Run only security tests
pytest -m security

# Run only performance tests
pytest -m performance

# Run only fast tests (exclude slow tests)
pytest -m "not slow"
```

## Test Configuration

### Pytest Configuration (`pytest.ini`)
- Test discovery patterns
- Output settings and markers
- Asyncio configuration
- Minimum Python version requirements

### Test Fixtures (`conftest.py`)
- Mock database configurations
- Mock plugin instances
- Sample test data (users, portfolios, market data)
- Test markers and collection hooks

## Test Data

### Mock Data Provided by Fixtures
- **Users**: Admin, trader, and readonly user profiles
- **Portfolios**: Sample portfolios with different configurations
- **Market Data**: Sample price data for testing strategies
- **Plugins**: Mock plugin instances for isolated testing

### Test Database
- Each test uses an isolated in-memory SQLite database
- Automatic cleanup after test completion
- Realistic schema matching production

## Test Quality Standards

### Coverage Requirements
- **Statement Coverage**: Minimum 95% for critical components
- **Branch Coverage**: Minimum 90% for decision points
- **Function Coverage**: 100% for public interfaces
- **Line Coverage**: Minimum 90% for production code

### Test Quality Metrics
- **Behavior-Driven**: All tests validate behaviors, not implementations
- **Implementation-Independent**: Tests work regardless of implementation changes
- **Realistic Data**: Tests use realistic scenarios and data
- **Comprehensive**: Full coverage of requirements and edge cases

### Performance Criteria
- **Unit Tests**: < 1 second per test
- **Integration Tests**: < 5 seconds per test
- **System Tests**: < 30 seconds per test
- **Acceptance Tests**: < 2 minutes per test

## Test Maintenance

### Regular Activities
- **Weekly**: Review test results and fix failing tests
- **Monthly**: Update test data and scenarios
- **Quarterly**: Review test coverage and add missing tests
- **Annually**: Review test strategy and methodology

### Test Updates
- Update tests when requirements change
- Add tests for new features and bug fixes
- Remove obsolete tests
- Refactor tests for maintainability

## Continuous Integration

### Test Execution in CI/CD
- Tests run automatically on every commit
- Different test levels run in parallel
- Failed tests block deployment
- Coverage reports generated for every build

### Test Reporting
- Detailed test execution reports
- Coverage analysis and trends
- Performance metrics tracking
- Security test results

## Troubleshooting

### Common Issues

1. **Import Errors**: Ensure all dependencies are installed
2. **Database Errors**: Check that test database is properly isolated
3. **Plugin Errors**: Verify mock plugins are properly configured
4. **Async Errors**: Ensure asyncio tests use proper fixtures

### Debug Tips

1. **Run with verbose output**: `python run_tests.py -v`
2. **Run specific test**: `pytest tests/unit/test_unit.py::TestClass::test_method`
3. **Use debugger**: Add `import pdb; pdb.set_trace()` in test code
4. **Check logs**: Enable debug logging in test configuration

## Test Plans

Each test level has a corresponding test plan document:

- **Acceptance**: `tests/plan_acceptance.md` - User stories and business requirements
- **System**: `tests/plan_system.md` - System-wide behaviors and architecture
- **Integration**: `tests/plan_integration.md` - Component interactions and interfaces
- **Unit**: `tests/plan_unit.md` - Individual component specifications

These plans provide detailed test cases, requirements traceability, and success criteria for each test level.

## Best Practices

### Writing Tests
1. **Follow the AAA Pattern**: Arrange, Act, Assert
2. **Use descriptive test names**: Clearly describe what is being tested
3. **Test one thing at a time**: Each test should validate one specific behavior
4. **Use realistic data**: Test with data that resembles production scenarios
5. **Test edge cases**: Include boundary conditions and error scenarios

### Test Organization
1. **Group related tests**: Use test classes to organize related test methods
2. **Use fixtures**: Share common setup code using pytest fixtures
3. **Mock external dependencies**: Isolate units under test from external systems
4. **Keep tests independent**: Each test should be able to run in isolation

### Test Maintenance
1. **Keep tests up to date**: Update tests when requirements change
2. **Remove obsolete tests**: Delete tests that are no longer relevant
3. **Refactor test code**: Keep test code clean and maintainable
4. **Monitor test performance**: Remove or optimize slow tests

---

*This test suite provides comprehensive coverage of the LTS system at all levels, ensuring reliability, security, and performance meet production requirements.*
