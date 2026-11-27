import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { AuditLogFilter, REVERSED_RANGE_MESSAGE } from '@/components/admin/audit/AuditLogFilter';

describe('AuditLogFilter', () => {
  const defaultFilters = {};
  const mockOnFilterChange = jest.fn();

  beforeEach(() => {
    jest.clearAllMocks();
  });

  it('renders all filter inputs (action_type, entity_type, date range)', () => {
    render(<AuditLogFilter filters={defaultFilters} onFilterChange={mockOnFilterChange} />);
    expect(screen.getByTestId('filter-action-type')).toBeInTheDocument();
    expect(screen.getByTestId('filter-entity-type')).toBeInTheDocument();
    expect(screen.getByTestId('filter-start-date')).toBeInTheDocument();
    expect(screen.getByTestId('filter-end-date')).toBeInTheDocument();
  });

  it('calls onFilterChange when action_type select changes', () => {
    render(<AuditLogFilter filters={defaultFilters} onFilterChange={mockOnFilterChange} />);
    fireEvent.change(screen.getByTestId('filter-action-type'), {
      target: { value: 'toggle_enable' },
    });
    expect(mockOnFilterChange).toHaveBeenCalledWith(
      expect.objectContaining({ action_type: 'toggle_enable' })
    );
  });

  it('calls onFilterChange when entity_type select changes', () => {
    render(<AuditLogFilter filters={defaultFilters} onFilterChange={mockOnFilterChange} />);
    fireEvent.change(screen.getByTestId('filter-entity-type'), {
      target: { value: 'feature_flag' },
    });
    expect(mockOnFilterChange).toHaveBeenCalledWith(
      expect.objectContaining({ entity_type: 'feature_flag' })
    );
  });

  // #665: a "User Email" box filtered nothing, because the API filters by
  // user id, not email. The bar has no user control.
  it('has no user email filter', () => {
    render(<AuditLogFilter filters={defaultFilters} onFilterChange={mockOnFilterChange} />);
    expect(screen.queryByTestId('filter-user-email')).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/user email/i)).not.toBeInTheDocument();
  });

  it('calls onFilterChange when start_date changes', () => {
    render(<AuditLogFilter filters={defaultFilters} onFilterChange={mockOnFilterChange} />);
    fireEvent.change(screen.getByTestId('filter-start-date'), {
      target: { value: '2024-01-01' },
    });
    expect(mockOnFilterChange).toHaveBeenCalledWith(
      expect.objectContaining({ start_date: '2024-01-01' })
    );
  });

  it('calls onFilterChange when end_date changes', () => {
    render(<AuditLogFilter filters={defaultFilters} onFilterChange={mockOnFilterChange} />);
    fireEvent.change(screen.getByTestId('filter-end-date'), {
      target: { value: '2024-01-31' },
    });
    expect(mockOnFilterChange).toHaveBeenCalledWith(
      expect.objectContaining({ end_date: '2024-01-31' })
    );
  });

  it('"Clear filters" button resets all filters and calls onFilterChange with empty object', () => {
    const filtersWithValues = {
      action_type: 'toggle_enable',
      entity_type: 'feature_flag',
      start_date: '2024-01-01',
      end_date: '2024-01-31',
    };
    render(<AuditLogFilter filters={filtersWithValues} onFilterChange={mockOnFilterChange} />);
    fireEvent.click(screen.getByTestId('clear-filters-button'));
    expect(mockOnFilterChange).toHaveBeenCalledWith({});
  });

  it('accepts a one-day range (To equal to From)', () => {
    render(<AuditLogFilter filters={{ start_date: '2024-01-10' }} onFilterChange={mockOnFilterChange} />);
    fireEvent.change(screen.getByTestId('filter-end-date'), { target: { value: '2024-01-10' } });
    expect(mockOnFilterChange).toHaveBeenCalledWith(
      expect.objectContaining({ start_date: '2024-01-10', end_date: '2024-01-10' })
    );
    expect(screen.queryByTestId('filter-date-error')).not.toBeInTheDocument();
  });

  it('refuses a To date before the From date, inline, without passing the range on', () => {
    render(<AuditLogFilter filters={{ start_date: '2024-01-10' }} onFilterChange={mockOnFilterChange} />);
    fireEvent.change(screen.getByTestId('filter-end-date'), { target: { value: '2024-01-09' } });
    expect(mockOnFilterChange).not.toHaveBeenCalled();
    expect(screen.getByTestId('filter-date-error')).toHaveTextContent(REVERSED_RANGE_MESSAGE);
    expect(screen.getByTestId('filter-end-date')).toHaveValue('2024-01-09');
    expect(screen.getByTestId('filter-end-date')).toHaveAttribute('aria-invalid', 'true');
  });

  it('refuses a From date moved past the To date', () => {
    render(<AuditLogFilter filters={{ end_date: '2024-01-10' }} onFilterChange={mockOnFilterChange} />);
    fireEvent.change(screen.getByTestId('filter-start-date'), { target: { value: '2024-01-11' } });
    expect(mockOnFilterChange).not.toHaveBeenCalled();
    expect(screen.getByTestId('filter-date-error')).toBeInTheDocument();
  });

  it('passes the range on once it is corrected, and clears the message', () => {
    render(<AuditLogFilter filters={{ start_date: '2024-01-10' }} onFilterChange={mockOnFilterChange} />);
    fireEvent.change(screen.getByTestId('filter-end-date'), { target: { value: '2024-01-09' } });
    fireEvent.change(screen.getByTestId('filter-end-date'), { target: { value: '2024-01-12' } });
    expect(mockOnFilterChange).toHaveBeenCalledTimes(1);
    expect(mockOnFilterChange).toHaveBeenCalledWith(
      expect.objectContaining({ start_date: '2024-01-10', end_date: '2024-01-12' })
    );
    expect(screen.queryByTestId('filter-date-error')).not.toBeInTheDocument();
  });

  it('renders with data-testid="audit-log-filter"', () => {
    render(<AuditLogFilter filters={defaultFilters} onFilterChange={mockOnFilterChange} />);
    expect(screen.getByTestId('audit-log-filter')).toBeInTheDocument();
  });
});
