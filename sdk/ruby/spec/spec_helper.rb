require 'webmock/rspec'
require_relative '../lib/controlvar'

RSpec.configure do |config|
  # Enable focused specs (fit/fdescribe)
  config.filter_run_when_matching :focus

  # Disable monkey patching — use explicit RSpec:: prefix if needed
  config.disable_monkey_patching!

  # Order randomization for independent specs
  config.order = :random
  Kernel.srand config.seed

  # Strict mocks
  config.mock_with :rspec do |mocks|
    mocks.verify_partial_doubles = true
  end

  # WebMock: disallow real HTTP by default in tests
  config.before(:suite) do
    WebMock.disable_net_connect!(allow_localhost: false)
  end

  config.after(:suite) do
    WebMock.allow_net_connect!
  end
end
