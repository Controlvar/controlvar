// Harness for the Go SDK's own ConsistentHash (see hash_contract.py). The
// replace directive points at the SDK in this checkout, so `go run .` compiles
// the SDK's source exactly as a consumer's build would.
module controlvar.local/sdkcontract

go 1.21

require github.com/controlvar/controlvar/sdk/go v0.0.0

replace github.com/controlvar/controlvar/sdk/go => ../../../../sdk/go
