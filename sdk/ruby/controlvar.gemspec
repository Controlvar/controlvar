require_relative 'lib/controlvar/version'

Gem::Specification.new do |s|
  s.name        = "controlvar"
  s.version     = Controlvar::VERSION
  s.summary     = "Ruby SDK for Controlvar A/B testing platform"
  s.description = "Native Ruby client for the Controlvar experimentation platform. " \
                  "Provides server-decided experiment assignment, feature flag evaluation, and " \
                  "event tracking with a thread-safe per-user TTL cache."
  s.authors     = ["Controlvar Platform Team"]
  s.email       = ["hello@controlvar.com"]
  s.homepage    = "https://github.com/Controlvar/controlvar"
  s.license     = "MIT"

  s.files            = Dir["lib/**/*.rb"] + ["README.md", "controlvar.gemspec"]
  s.require_paths    = ["lib"]

  s.required_ruby_version = ">= 2.6.0"

  # No runtime dependencies — stdlib only
  # Uses: Digest (MD5), Net::HTTP, JSON, Mutex, Struct, Thread
  #
  # Development / test dependencies are in the Gemfile (group :development, :test)
end
