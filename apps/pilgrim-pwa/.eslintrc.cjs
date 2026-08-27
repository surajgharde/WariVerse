/* eslint-env node */
module.exports = {
  root: true,
  env: { browser: true, es2022: true, serviceworker: true },
  parser: '@typescript-eslint/parser',
  parserOptions: { ecmaVersion: 'latest', sourceType: 'module', ecmaFeatures: { jsx: true } },
  plugins: ['@typescript-eslint'],
  extends: ['eslint:recommended', 'plugin:@typescript-eslint/recommended'],
  ignorePatterns: ['dist', 'node_modules', '*.cjs', 'public/sw.js', 'scripts'],
  rules: {
    '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
    eqeqeq: ['error', 'always', { null: 'ignore' }],
    // The pilgrim app has no console. Anything worth logging is worth showing.
    'no-console': 'error',
  },
}
