// Harness: the Go SDK's own ConsistentHash.
//
// Reads {"inputs": [[userID, flagKey], ...]} on stdin and prints
// {"hashes": [...], "md5_hex": null}; hash_contract.py does the comparing.
package main

import (
	"encoding/json"
	"fmt"
	"os"

	experimentation "github.com/controlvar/controlvar/sdk/go"
)

func main() {
	var req struct {
		Inputs [][2]string `json:"inputs"`
	}
	if err := json.NewDecoder(os.Stdin).Decode(&req); err != nil {
		fmt.Fprintln(os.Stderr, "reading inputs:", err)
		os.Exit(1)
	}
	hashes := make([]float64, 0, len(req.Inputs))
	for _, in := range req.Inputs {
		hashes = append(hashes, experimentation.ConsistentHash(in[0], in[1]))
	}
	out := map[string]any{"hashes": hashes, "md5_hex": nil}
	if err := json.NewEncoder(os.Stdout).Encode(out); err != nil {
		fmt.Fprintln(os.Stderr, "writing result:", err)
		os.Exit(1)
	}
}
