// Иконка приложения («светлячок»): тёмный фон, жёлто-зелёная точка с мягким
// ореолом. Рисуется в пределах центральных ~46% холста, поэтому один и тот же
// рисунок годится и для purpose "any", и для "maskable" (безопасная зона 80%).
export function PwaIcon({ size }: { size: number }) {
  const halo = Math.round(size * 0.46);
  const glow = Math.round(size * 0.34);
  const dot = Math.round(size * 0.2);

  return (
    <div
      style={{
        width: size,
        height: size,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: "#0a0a0a",
      }}
    >
      <div
        style={{
          width: halo,
          height: halo,
          borderRadius: "50%",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          background: "rgba(212, 245, 66, 0.12)",
        }}
      >
        <div
          style={{
            width: glow,
            height: glow,
            borderRadius: "50%",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            background: "rgba(212, 245, 66, 0.28)",
          }}
        >
          <div
            style={{
              width: dot,
              height: dot,
              borderRadius: "50%",
              background: "#d4f542",
            }}
          />
        </div>
      </div>
    </div>
  );
}
