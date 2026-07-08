export default function Toast({ message }) {
  return (
    <div
      className={`pointer-events-none fixed right-5 bottom-5 z-50 rounded-xl bg-slate-900/90 px-4
                  py-2.5 text-[12.5px] font-medium text-white shadow-lg transition-all duration-300
                  dark:bg-[#2a2d3e]
                  ${message ? "translate-y-0 opacity-100" : "translate-y-2 opacity-0"}`}
    >
      {message}
    </div>
  );
}
