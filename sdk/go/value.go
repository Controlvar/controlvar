package experimentation

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
)

// The value types a flag can have, as the evaluate route names them in
// value_type. A boolean flag's value is Enabled; it has no accessor here.
const (
	valueTypeString = "string"
	valueTypeNumber = "number"
	valueTypeJSON   = "json"
)

// StringValue returns the value the flag serves this user when it is a
// string flag (ValueType "string"). It returns def instead when r is nil,
// when the server sent no value (a server without flag values, or a failed
// evaluation), when the flag is of another type, or when the value is not a
// JSON string. It never panics.
func (r *EvalResult) StringValue(def string) string {
	if r == nil || r.ValueType != valueTypeString || !hasValue(r.Value) {
		return def
	}
	var s string
	if err := json.Unmarshal(r.Value, &s); err != nil {
		return def
	}
	return s
}

// NumberValue returns the value the flag serves this user when it is a
// number flag (ValueType "number"). It returns def instead when r is nil,
// when the server sent no value (a server without flag values, or a failed
// evaluation), when the flag is of another type, or when the value is not a
// JSON number that fits a float64. It never panics.
func (r *EvalResult) NumberValue(def float64) float64 {
	if r == nil || r.ValueType != valueTypeNumber || !hasValue(r.Value) {
		return def
	}
	var f float64
	if err := json.Unmarshal(r.Value, &f); err != nil {
		return def
	}
	return f
}

// DecodeValue decodes the value the flag serves this user into v, a non-nil
// pointer, when it is a json flag (ValueType "json"; the value is a JSON
// object or array). On success v holds the decoded value and DecodeValue
// returns nil: the value replaces what v held, so a field the value does not
// name is left at its zero value, not merged.
//
// Otherwise DecodeValue returns an error and leaves v as it was, so a default
// you put in v first is what you keep: when r is nil, when the server sent no
// value (a server without flag values, or a failed evaluation), when the flag
// is of another type, when v is not a non-nil pointer, or when the value does
// not decode into v's type. It never panics.
func (r *EvalResult) DecodeValue(v any) error {
	if r == nil {
		return errors.New("experimentation: DecodeValue on a nil *EvalResult")
	}
	target := reflect.ValueOf(v)
	if target.Kind() != reflect.Pointer || target.IsNil() {
		return &json.InvalidUnmarshalError{Type: reflect.TypeOf(v)}
	}
	if !hasValue(r.Value) {
		return fmt.Errorf("experimentation: flag %q: the server sent no value", r.Key)
	}
	if r.ValueType != valueTypeJSON {
		return fmt.Errorf("experimentation: flag %q has value type %q, not %q", r.Key, r.ValueType, valueTypeJSON)
	}
	// Decode into a new value of v's type and copy it over only on success, so
	// a failed decode cannot leave v partly filled.
	decoded := reflect.New(target.Elem().Type())
	if err := json.Unmarshal(r.Value, decoded.Interface()); err != nil {
		return fmt.Errorf("experimentation: flag %q: decode value: %w", r.Key, err)
	}
	target.Elem().Set(decoded.Elem())
	return nil
}

// hasValue reports whether raw holds a value: nothing, and a JSON null, are
// no value.
func hasValue(raw json.RawMessage) bool {
	trimmed := bytes.TrimSpace(raw)
	return len(trimmed) > 0 && !bytes.Equal(trimmed, []byte("null"))
}

// clone returns a copy of r whose Value has bytes of its own, so a caller who
// changes the result it was given changes nothing in the cache. Config is
// shared, as it always was (the server sends null for it).
func (r *EvalResult) clone() *EvalResult {
	c := *r
	if r.Value != nil {
		c.Value = append(json.RawMessage(nil), r.Value...)
	}
	return &c
}
