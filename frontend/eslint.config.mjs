import tseslint from "@typescript-eslint/eslint-plugin";
import tsparser from "@typescript-eslint/parser";
import lit from "eslint-plugin-lit";

export default [
  {
    files: ["src/**/*.ts"],
    ignores: ["src/**/*.d.ts"],
    languageOptions: {
      parser: tsparser,
      parserOptions: {
        ecmaVersion: 2021,
        sourceType: "module",
      },
    },
    plugins: {
      "@typescript-eslint": tseslint,
      lit,
    },
    rules: {
      ...tseslint.configs.recommended.rules,
      ...lit.configs.recommended.rules,
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_" }],
      "@typescript-eslint/no-explicit-any": "warn",
      "@typescript-eslint/explicit-function-return-type": "off",
      "no-console": "warn",
    },
  },
];
