import { ImageResponse } from "next/og";
import { PwaIcon } from "@/lib/pwa-icon";

// Стабильные URL для manifest.ts (у file-convention icon.tsx адреса содержат хеш).
const SIZES = [192, 512] as const;

export function generateStaticParams() {
  return SIZES.map((size) => ({ size: String(size) }));
}

export async function GET(_request: Request, ctx: RouteContext<"/pwa-icon/[size]">) {
  const { size } = await ctx.params;
  const px = SIZES.find((s) => String(s) === size);
  if (!px) {
    return new Response("Not found", { status: 404 });
  }
  return new ImageResponse(<PwaIcon size={px} />, { width: px, height: px });
}
