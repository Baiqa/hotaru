import type { Metadata } from "next";

// Служебная зона: не индексируется. Проверка доступа выполняется в самих страницах
// (requireAdmin), layout только задаёт метаданные.
export const metadata: Metadata = {
  title: "Hotaru — админка",
  robots: { index: false, follow: false },
};

export default function AdminLayout({ children }: { children: React.ReactNode }) {
  return <>{children}</>;
}
