/** @type {import('next').NextConfig} */
const config = {
  reactStrictMode: true,
  eslint: { ignoreDuringBuilds: true },
  // The SSE endpoint is a long-lived response; give the proxy room.
  experimental: { proxyTimeout: 1000 * 60 * 30 },
};

export default config;
