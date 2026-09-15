import js from "@eslint/js";
import jsxA11y from "eslint-plugin-jsx-a11y";
import reactHooks from "eslint-plugin-react-hooks";
import simpleImportSort from "eslint-plugin-simple-import-sort";
import globals from "globals";
import tseslint from "typescript-eslint";

const UNUSED_IGNORE = {
  args: "all",
  argsIgnorePattern: "^_",
  varsIgnorePattern: "^_",
  caughtErrorsIgnorePattern: "^_",
};

export default tseslint.config(
  { ignores: ["dist/**", "node_modules/**", "coverage/**", "scripts/**", "*.config.*"] },
  {
    files: ["public-src/**/*.{ts,tsx}"],
    extends: [
      js.configs.recommended,
      ...tseslint.configs.recommendedTypeChecked,
      ...tseslint.configs.stylisticTypeChecked,
    ],
    languageOptions: {
      parserOptions: { projectService: true, tsconfigRootDir: import.meta.dirname },
      globals: { ...globals.browser },
    },
    plugins: { "react-hooks": reactHooks, "jsx-a11y": jsxA11y, "simple-import-sort": simpleImportSort },
    rules: {
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "warn",
      ...jsxA11y.flatConfigs.recommended.rules,
      "simple-import-sort/imports": "error",
      "simple-import-sort/exports": "error",
      "@typescript-eslint/no-unused-vars": ["error", UNUSED_IGNORE],
      complexity: ["error", 15],
      "max-depth": ["error", 4],
      "max-lines": ["error", { max: 500, skipBlankLines: true, skipComments: true }],
      "max-lines-per-function": ["error", { max: 80, skipBlankLines: true, skipComments: true }],
    },
  },
  {
    files: ["public-src/**/*.tsx"],
    rules: {
      "max-lines-per-function": ["error", { max: 150, skipBlankLines: true, skipComments: true }],
    },
  },
  {
    files: [
      "public-src/**/*.{test,spec}.{ts,tsx}",
      "public-src/**/*.stories.{ts,tsx}",
      "public-src/test-setup.ts",
    ],
    rules: {
      complexity: "off",
      "max-depth": "off",
      "max-lines": "off",
      "max-lines-per-function": "off",
      // Test doubles, jsdom polyfills and testing-library patterns legitimately trip these.
      "@typescript-eslint/no-empty-function": "off",
      "@typescript-eslint/require-await": "off",
      "@typescript-eslint/unbound-method": "off",
      "@typescript-eslint/no-unnecessary-type-assertion": "off",
      "@typescript-eslint/non-nullable-type-assertion-style": "off",
    },
  },
);
