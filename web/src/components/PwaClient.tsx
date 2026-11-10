"use client";

import { usePathname } from "next/navigation";
import { useEffect, useRef, useSyncExternalStore } from "react";

function subscribeOnline(callback: () => void) {
  window.addEventListener("online", callback);
  window.addEventListener("offline", callback);
  return () => {
    window.removeEventListener("online", callback);
    window.removeEventListener("offline", callback);
  };
}

// Регистрирует Service Worker (только в production), просит его сохранять
// страницы при мягкой навигации и показывает полосу при отсутствии сети.
export function PwaClient() {
  const pathname = usePathname();
  const firstRender = useRef(true);
  const online = useSyncExternalStore(
    subscribeOnline,
    () => navigator.onLine,
    () => true,
  );

  useEffect(() => {
    if (process.env.NODE_ENV !== "production" || !("serviceWorker" in navigator)) return;
    navigator.serviceWorker
      .register("/sw.js", { scope: "/", updateViaCache: "none" })
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    if (!("serviceWorker" in navigator)) return;
    // Первая загрузка — полная навигация, её worker сохраняет сам. Исключение —
    // самый первый визит: страница открылась до того, как worker взял её под
    // контроль, поэтому просим сохранить и её.
    let firstVisit = false;
    if (firstRender.current) {
      firstRender.current = false;
      if (navigator.serviceWorker.controller) return;
      firstVisit = true;
    }
    navigator.serviceWorker.ready
      .then((registration) => {
        registration.active?.postMessage({ type: "cache-page", url: window.location.href });
        if (firstVisit) {
          const urls = performance
            .getEntriesByType("resource")
            .map((entry) => entry.name)
            .filter((name) => name.includes("/_next/static/"));
          registration.active?.postMessage({ type: "cache-assets", urls });
        }
      })
      .catch(() => undefined);
  }, [pathname]);

  if (online) return null;

  return (
    <div
      role="status"
      className="fixed inset-x-0 bottom-0 z-50 bg-zinc-900 px-4 py-2 text-center text-sm text-zinc-50 dark:bg-zinc-100 dark:text-zinc-900"
      style={{ paddingBottom: "max(0.5rem, env(safe-area-inset-bottom))" }}
    >
      Нет сети. Показаны сохранённые материалы.
    </div>
  );
}
