# Swagger UI, served by this machine

The script, the style and the icon of the `/docs` page, so that it works on a
network with no way out (until 2026-10-06 the page loaded them from
cdn.jsdelivr.net in the browser, and stayed blank without internet).

- Package: `swagger-ui-dist` 5.33.1, by SmartBear Software, under the Apache
  License 2.0 (`LICENSE`, `NOTICE` beside these files, unmodified).
- Taken from the npm registry tarball
  `https://registry.npmjs.org/swagger-ui-dist/-/swagger-ui-dist-5.33.1.tgz`,
  checked against its published integrity
  `sha512-H872wWkA53bFIsGgi7OWgmq+CRWw3nFQGdJWRqOB9wNwTm6e5ol34+qPDkV4AJlK+gglM2EsJiOOzsGsEGbluA==`.
- Files kept: `swagger-ui-bundle.js`, `swagger-ui.css`, `favicon-32x32.png`,
  byte for byte as in the package.

To move to another version: the same three files from the new tarball, its
integrity checked, and the version updated above and where it is quoted:
docs/guide/api.md ("Talking to it"), `NOTICE` and `docs/DEPENDENCIES.md`.
