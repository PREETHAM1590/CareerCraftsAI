/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  compress: true,
  poweredByHeader: false,
  reactStrictMode: true,
  turbopack: {
    root: __dirname,
  },

  // Optimize images
  images: {
    formats: ["image/avif", "image/webp"],
    minimumCacheTTL: 60 * 60 * 24 * 30, // 30 days
    remotePatterns: [
      {
        protocol: "https",
        hostname: "i.ytimg.com",
      },
    ],
  },

  // Proxy /api/v1/* → backend. Browser only ever talks to localhost:3000,
  // so the frontend bundle never needs to know the backend's docker DNS
  // name (which the host browser can't resolve).
  //
  // In docker (frontend container), BACKEND_URL=http://backend:8000
  // is set by docker-compose and rewrites go through the docker network.
  //
  // In local dev (`npm run dev` on the host), set BACKEND_URL=http://localhost:8000
  // in frontend/.env.local so the rewrite points at the host-local uvicorn.
  async rewrites() {
    const backendUrl = process.env.BACKEND_URL || "http://backend:8000";
    return [
      {
        source: "/api/v1/:path*",
        destination: `${backendUrl}/api/v1/:path*`,
      },
      // The public /status page reads the API's health from its own origin.
      {
        source: "/health",
        destination: `${backendUrl}/health`,
      },
    ];
  },

  // Interview Prep merged into /interview (Prep plan tab). Redirect at the
  // routing layer so old links get a real HTTP redirect, not a client-side one.
  async redirects() {
    return [
      {
        source: "/interview-prep",
        destination: "/interview?tab=prep",
        permanent: false,
      },
      // The chat agent was renamed from Copilot to Nova; keep old links working.
      {
        source: "/copilot/:path*",
        destination: "/nova/:path*",
        permanent: true,
      },
    ];
  },

  // Cache public assets. Next.js manages /_next/static cache headers itself,
  // which keeps dev chunks from being pinned across Fast Refresh rebuilds.
  async headers() {
    return [
      {
        // The app has approve and submit controls: it must not be framed.
        source: "/:path*",
        headers: [
          { key: "X-Frame-Options", value: "SAMEORIGIN" },
          { key: "Content-Security-Policy", value: "frame-ancestors 'self'" },
        ],
      },
      {
        source: "/:all*(svg|jpg|png|webp|avif|woff2)",
        headers: [
          { key: "Cache-Control", value: "public, max-age=31536000, immutable" },
        ],
      },
    ];
  },
};

module.exports = nextConfig;
