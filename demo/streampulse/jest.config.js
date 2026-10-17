module.exports = {
  testEnvironment: 'jsdom',
  setupFilesAfterEnv: ['<rootDir>/jest.setup.ts'],
  transform: {
    '^.+\\.(ts|tsx)$': [
      'ts-jest',
      {
        // Transpile only. The SDK is consumed from source and its public surface is
        // type-checked by `npm run typecheck`, not by the test run.
        diagnostics: false,
        tsconfig: {
          jsx: 'react-jsx',
          module: 'commonjs',
        },
      },
    ],
  },
  moduleNameMapper: {
    // Tests never talk to the real SDK: this manual mock exposes configurable hook
    // return values (see src/__mocks__/controlvar-sdk.tsx).
    '^@controlvar/react-sdk$': '<rootDir>/src/__mocks__/controlvar-sdk.tsx',
    // Next throws without a mounted router, so use a stub.
    '^next/router$': '<rootDir>/src/__mocks__/next-router.ts',
    '^@/(.*)$': '<rootDir>/src/$1',
    '\\.(css|less|scss|png|jpg|jpeg|gif|svg)$': 'identity-obj-proxy',
  },
  testMatch: ['<rootDir>/src/**/*.test.ts', '<rootDir>/src/**/*.test.tsx'],
  testPathIgnorePatterns: ['/node_modules/', '/.next/', '/out/', '/simulator/'],
  collectCoverageFrom: ['src/**/*.{ts,tsx}', '!src/**/*.d.ts', '!src/__mocks__/**', '!src/__tests__/**'],
};
