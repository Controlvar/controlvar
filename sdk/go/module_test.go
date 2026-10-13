package experimentation_test

import (
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The module requires nothing: the SDK is standard library only.
func TestModule_StandardLibraryOnly(t *testing.T) {
	data, err := os.ReadFile("go.mod")
	if err != nil {
		t.Fatalf("read go.mod: %v", err)
	}
	if !strings.HasPrefix(string(data), "module github.com/getexperimently/experimently/sdk/go\n") {
		t.Fatalf("go.mod does not declare the module path the docs give:\n%s", data)
	}
	for _, line := range strings.Split(string(data), "\n") {
		if strings.HasPrefix(strings.TrimSpace(line), "require") {
			t.Fatalf("go.mod requires a module: %q", line)
		}
	}
	fset := token.NewFileSet()
	sources, _ := filepath.Glob("*.go")
	checked := 0
	for _, path := range sources {
		if strings.HasSuffix(path, "_test.go") {
			continue
		}
		file, err := parser.ParseFile(fset, path, nil, parser.ImportsOnly)
		if err != nil {
			t.Fatalf("parse %s: %v", path, err)
		}
		for _, spec := range file.Imports {
			first := strings.SplitN(strings.Trim(spec.Path.Value, `"`), "/", 2)[0]
			if strings.Contains(first, ".") {
				t.Errorf("%s imports %s, which is not in the standard library", path, spec.Path.Value)
			}
		}
		checked++
	}
	if checked < 3 {
		t.Fatalf("checked %d source files: the scan is broken", checked)
	}
}

// Nothing in the SDK calls ConsistentHash: the server decides every variant and flag.
func TestConsistentHash_NotCalledByTheClient(t *testing.T) {
	fset := token.NewFileSet()
	sources, _ := filepath.Glob("*.go")
	declared := false
	for _, path := range sources {
		if strings.HasSuffix(path, "_test.go") {
			continue
		}
		file, err := parser.ParseFile(fset, path, nil, 0)
		if err != nil {
			t.Fatalf("parse %s: %v", path, err)
		}
		ast.Inspect(file, func(node ast.Node) bool {
			switch n := node.(type) {
			case *ast.FuncDecl:
				if n.Name.Name == "ConsistentHash" {
					declared = true
				}
			case *ast.CallExpr:
				if ident, ok := n.Fun.(*ast.Ident); ok && ident.Name == "ConsistentHash" {
					t.Errorf("%s calls ConsistentHash at %s", path, fset.Position(n.Pos()))
				}
			}
			return true
		})
	}
	if !declared {
		t.Fatal("found no ConsistentHash declaration: the scan is broken")
	}
}

// The module's go directive is the oldest Go it builds with: 1.21.
func TestModule_RequiresGo121(t *testing.T) {
	data, err := os.ReadFile("go.mod")
	if err != nil {
		t.Fatalf("read go.mod: %v", err)
	}
	directives := 0
	for _, line := range strings.Split(string(data), "\n") {
		if fields := strings.Fields(line); len(fields) == 2 && fields[0] == "go" {
			directives++
			if fields[1] != "1.21" {
				t.Errorf("go.mod says go %s; the SDK's pages say Go 1.21+", fields[1])
			}
		}
	}
	if directives != 1 {
		t.Fatalf("go.mod has %d go directives, not one", directives)
	}
}
