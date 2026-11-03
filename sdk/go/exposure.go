package experimentation

import (
	"context"
	"errors"
	"fmt"
)

// errExposureHook is the kind of every error callExposureHook makes of a
// panic in the function given to WithOnExposure, as HOOK_ERROR is in the
// JavaScript SDK: errors.Is(err, errExposureHook) holds for such an error and
// for no other the SDK makes.
var errExposureHook = errors.New("onExposure failed")

// callExposureHook calls fn with ctx and e, and is the only place the client
// calls it. It recovers a panic in fn, so nothing fn does reaches the caller
// of GetAssignment or changes its answer, and returns it as an error of the
// errExposureHook kind; it returns nil when fn returns. A fatal runtime error
// (a concurrent map write, a stack overflow) is not a panic and cannot be
// recovered.
func callExposureHook(ctx context.Context, fn func(context.Context, Exposure), e Exposure) (err error) {
	defer func() {
		if recovered := recover(); recovered != nil {
			err = exposureError(recovered)
		}
	}()
	fn(ctx, e)
	return nil
}

// exposureError is the error for what the exposure function panicked with:
// its text is "onExposure failed: " and the panic's, and it wraps the panic
// value with %w when that is an error. Formatting the value calls its Error or
// String method, which may panic again, and fmt re-panics on a nested panic; a
// panic while formatting is recovered here and gives the fixed text
// "onExposure failed: the panic value cannot be turned into a string", so it
// never reaches the caller of GetAssignment.
func exposureError(recovered any) (err error) {
	defer func() {
		if recover() != nil {
			err = fmt.Errorf("%w: the panic value cannot be turned into a string", errExposureHook)
		}
	}()
	if cause, ok := recovered.(error); ok {
		return fmt.Errorf("%w: %w", errExposureHook, cause)
	}
	return fmt.Errorf("%w: %v", errExposureHook, recovered)
}
