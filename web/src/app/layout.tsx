import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import Link from "next/link";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Hotaru",
  description: "Новости как структура: факты с атрибуцией, а не пересказ статей.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="ru"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="min-h-full flex flex-col bg-white dark:bg-black">
        <header className="border-b border-zinc-200 px-4 py-4 sm:px-6 dark:border-zinc-800">
          <div className="mx-auto max-w-2xl">
            <Link href="/" className="text-lg font-bold text-zinc-900 dark:text-zinc-50">
              Hotaru
            </Link>
          </div>
        </header>
        {children}
      </body>
    </html>
  );
}
