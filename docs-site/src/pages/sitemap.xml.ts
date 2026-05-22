import type { APIRoute } from "astro";
import { getCollection } from "astro:content";

const SITE = "https://plynf.com";

export const GET: APIRoute = async () => {
  const docs = await getCollection("docs");
  const today = new Date().toISOString().slice(0, 10);

  const urls: { loc: string; priority: number }[] = [
    { loc: "/", priority: 1.0 },
    { loc: "/why", priority: 0.8 },
    { loc: "/pricing", priority: 0.6 },
    { loc: "/docs/", priority: 0.9 },
    ...docs.map((d) => ({ loc: `/docs/${d.slug}`, priority: 0.7 })),
  ];

  const body = `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n${urls
    .map(
      (u) =>
        `  <url>\n    <loc>${SITE}${u.loc}</loc>\n    <lastmod>${today}</lastmod>\n    <priority>${u.priority.toFixed(1)}</priority>\n  </url>`,
    )
    .join("\n")}\n</urlset>\n`;

  return new Response(body, {
    headers: { "Content-Type": "application/xml; charset=utf-8" },
  });
};

export const prerender = true;
