// Mirrors the PDS frontend settings that matter for consuming a package: Pages Router, `next build --webpack`,
// React strict mode and experimental.esmExternals off. (PDS also wraps this in Sentry and adds rewrites/headers,
// which do not affect package consumption.)
module.exports = {
  reactStrictMode: true,
  experimental: { esmExternals: false },
}
