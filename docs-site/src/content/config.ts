import { defineCollection, z } from "astro:content";

const docs = defineCollection({
  type: "content",
  schema: z.object({
    title: z.string(),
    description: z.string().optional(),
    order: z.number().default(99),
    section: z
      .enum(["overview", "guides", "api", "operations"])
      .default("guides"),
    sourceFile: z.string().optional(),
  }),
});

export const collections = { docs };
