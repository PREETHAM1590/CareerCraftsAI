import type { Metadata } from "next";
import { DM_Sans, Instrument_Serif, Playfair_Display, Outfit, Geist, Geist_Mono } from "next/font/google";
import { ClerkProvider } from "@clerk/nextjs";
import { Providers } from "@/components/layout/Providers";
import { JsonLd } from "@/components/seo/JsonLd";
import "./globals.css";

// Inter was downloaded on every page load but never used: it sat only as a
// fallback behind DM Sans in the sans stack, so it rendered only if DM Sans
// failed. It is also the single most over-used typeface in generated UI.
// Dropped — the stack falls through to ui-sans-serif/system-ui instead.
const dmSans = DM_Sans({
  subsets: ["latin"],
  weight: ["300", "400", "500", "600"],
  variable: "--font-dm-sans",
  display: "swap",
});
const instrumentSerif = Instrument_Serif({
  subsets: ["latin"],
  weight: "400",
  style: "italic",
  variable: "--font-instrument-serif",
  display: "swap",
});
const playfair = Playfair_Display({
  subsets: ["latin"],
  weight: ["500", "600", "700"],
  style: ["normal", "italic"],
  variable: "--font-playfair",
  display: "swap",
});
// In-app command-center headlines (CommandHeader, metrics, sidebar wordmark)
// use this instead of the serif `display`/`hero` faces — serif reads as
// editorial/marketing, not functional software UI, on those surfaces.
const outfit = Outfit({
  subsets: ["latin"],
  weight: ["500", "600", "700"],
  variable: "--font-outfit",
  display: "swap",
});

// Vanguard in-app redesign: Geist for app-screen UI and headlines, Geist Mono
// for run IDs, logs and tabular data. Marketing pages keep their own faces.
const geist = Geist({
  subsets: ["latin"],
  variable: "--font-geist",
  display: "swap",
});
const geistMono = Geist_Mono({
  subsets: ["latin"],
  variable: "--font-geist-mono",
  display: "swap",
});

const SITE_URL = "https://careercraftsai.me";
const SITE_NAME = "CareerCraft AI";
const SITE_DESCRIPTION =
  "Nova, the AI job-search agent for students and freshers. Tailor resumes, match jobs, and follow up — automatically.";
const DEFAULT_TITLE = "CareerCraft AI — Apply smarter. Tailor faster.";

export const metadata: Metadata = {
  metadataBase: new URL(SITE_URL),
  title: {
    default: DEFAULT_TITLE,
    template: `%s — ${SITE_NAME}`,
  },
  description: SITE_DESCRIPTION,
  alternates: {
    canonical: "/",
  },
  openGraph: {
    type: "website",
    siteName: SITE_NAME,
    url: SITE_URL,
    title: DEFAULT_TITLE,
    description: SITE_DESCRIPTION,
  },
  twitter: {
    card: "summary_large_image",
    title: DEFAULT_TITLE,
    description: SITE_DESCRIPTION,
  },
};

// Omits `address` and `sameAs` — no public street address exists yet (see
// the Privacy Policy's contact section) and no verified social profiles
// exist either. Fabricating either would be worse than leaving them out.
const ORGANIZATION_JSON_LD = {
  "@context": "https://schema.org",
  "@type": "Organization",
  name: SITE_NAME,
  url: SITE_URL,
  logo: `${SITE_URL}/icon.svg`,
  description: SITE_DESCRIPTION,
};

const WEBSITE_JSON_LD = {
  "@context": "https://schema.org",
  "@type": "WebSite",
  name: SITE_NAME,
  url: SITE_URL,
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    // No Clerk prebuilt UI is rendered anywhere in this app (no <SignIn/>,
    // <SignUp/> or <UserButton/>), so no "Secured by Clerk" badge appears.
    // ClerkProvider only supplies session context to the headless hooks.
    <ClerkProvider signInUrl="/login" signUpUrl="/login">
      <html lang="en" className={`${dmSans.variable} ${instrumentSerif.variable} ${playfair.variable} ${outfit.variable} ${geist.variable} ${geistMono.variable}`} suppressHydrationWarning>
        <body className="font-sans antialiased">
          <JsonLd data={ORGANIZATION_JSON_LD} />
          <JsonLd data={WEBSITE_JSON_LD} />
          <Providers>{children}</Providers>
        </body>
      </html>
    </ClerkProvider>
  );
}
