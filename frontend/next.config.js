// Content-Security-Policy for static (non-nonce) rendering. Next.js App Router
// emits inline bootstrap scripts and Clerk injects runtime styles, so
// 'unsafe-inline' stays on script-src/style-src (per Clerk's manual-CSP
// guidance); everything else is pinned to known origins. A nonce policy would
// force every page to render per request.
const isDev = process.env.NODE_ENV !== "production";

function clerkFrontendHost() {
  const key = process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY || "";
  try {
    const host = Buffer.from(key.split("_").slice(2).join("_"), "base64").toString("utf8").replace(/\$$/, "");
    if (/^[a-z0-9.-]+\.[a-z]{2,}$/i.test(host)) return `https://${host}`;
  } catch {
    /* fall through to the development-instance wildcard */
  }
  return "https://*.clerk.accounts.dev";
}

function apiOrigin() {
  try {
    return new URL(process.env.NEXT_PUBLIC_API_URL || "").origin;
  } catch {
    return "";
  }
}

function contentSecurityPolicy() {
  const clerk = clerkFrontendHost();
  const protect = "https://*.protect.clerk.com";
  const turnstile = "https://challenges.cloudflare.com";
  const directives = {
    "default-src": ["'self'"],
    "script-src": ["'self'", "'unsafe-inline'", ...(isDev ? ["'unsafe-eval'"] : []), clerk, turnstile, protect],
    "style-src": ["'self'", "'unsafe-inline'"],
    "img-src": ["'self'", "data:", "blob:", "https:"],
    "font-src": ["'self'", "data:"],
    "media-src": ["'self'", "blob:"],
    "connect-src": ["'self'", apiOrigin(), clerk, "https://*.protect.clerk.com:*", ...(isDev ? ["ws:", "http://localhost:*"] : [])],
    "frame-src": ["'self'", turnstile, protect],
    "worker-src": ["'self'", "blob:"],
    "manifest-src": ["'self'"],
    "object-src": ["'none'"],
    "base-uri": ["'self'"],
    "form-action": ["'self'"],
    "frame-ancestors": ["'self'"],
  };
  const policy = Object.entries(directives).map(([name, values]) => `${name} ${values.filter(Boolean).join(" ")}`);
  return policy.join("; ");
}

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
          { key: "Content-Security-Policy", value: contentSecurityPolicy() },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=(), payment=(), usb=()" },
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
