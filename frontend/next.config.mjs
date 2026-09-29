import type { Config } from "next";

const config: Config = {
  reactStrictMode: true,
  eslint: { ignoreDuringBuilds: true },
  experimental: {
    // Keep the SSE endpoint a plain Node runtime: it is a long-lived response.
    proxyTimeout: 1000 * 60 * 30,
  },
};

export default config;
