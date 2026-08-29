/* Inline SVG icon set (lucide outlines), shared across views. */

const base = {
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.8,
  strokeLinecap: "round",
  strokeLinejoin: "round",
};

export const CodeIcon = (props) => (
  <svg {...base} {...props}>
    <path d="m8 6-6 6 6 6M16 6l6 6-6 6" />
  </svg>
);

export const UsersIcon = (props) => (
  <svg {...base} {...props}>
    <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" />
    <circle cx="9" cy="7" r="4" />
    <path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75" />
  </svg>
);

export const UserIcon = (props) => (
  <svg {...base} {...props}>
    <path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2" />
    <circle cx="12" cy="7" r="4" />
  </svg>
);

/* One person plus a plus: "make a new one", as against UsersIcon's
   "the accounts you have". */
export const UserPlusIcon = (props) => (
  <svg {...base} {...props}>
    <path d="M15 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" />
    <circle cx="8.5" cy="7" r="4" />
    <path d="M19 8v6M22 11h-6" />
  </svg>
);

export const GearIcon = (props) => (
  <svg {...base} {...props}>
    <path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z" />
    <circle cx="12" cy="12" r="3" />
  </svg>
);

export const FileIcon = (props) => (
  <svg {...base} {...props}>
    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z" />
    <path d="M14 2v6h6" />
  </svg>
);

export const RocketIcon = (props) => (
  <svg {...base} {...props}>
    <path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z" />
    <path d="m12 15-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z" />
    <path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5" />
  </svg>
);

export const PlayIcon = (props) => (
  <svg viewBox="0 0 24 24" fill="currentColor" {...props}>
    <path d="M7 4.6c0-.8.87-1.29 1.55-.88l11.2 6.9a1 1 0 0 1 0 1.7l-11.2 6.9A1.03 1.03 0 0 1 7 18.34Z" />
  </svg>
);

export const StopIcon = (props) => (
  <svg viewBox="0 0 24 24" fill="currentColor" {...props}>
    <rect x="6" y="6" width="12" height="12" rx="2" />
  </svg>
);

export const MonitorIcon = (props) => (
  <svg {...base} {...props}>
    <rect x="2" y="3" width="20" height="14" rx="2" />
    <path d="M8 21h8M12 17v4" />
  </svg>
);

/* The same monitor, struck through: "put this window away". The stroke is a
   separate path so it inherits the same colour and width as the frame rather
   than reading as a second, unrelated mark. */
export const MonitorOffIcon = (props) => (
  <svg {...base} {...props}>
    <rect x="2" y="3" width="20" height="14" rx="2" />
    <path d="M8 21h8M12 17v4" />
    <path d="m3 2 18 18" />
  </svg>
);

export const TrashIcon = (props) => (
  <svg {...base} {...props}>
    <path d="M3 6h18" />
    <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" />
    <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
  </svg>
);

export const PlusIcon = (props) => (
  <svg {...base} strokeWidth={2.1} {...props}>
    <path d="M12 5v14M5 12h14" />
  </svg>
);

export const MinusIcon = (props) => (
  <svg {...base} {...props}>
    <path d="M5 12h14" />
  </svg>
);

export const MaximizeIcon = (props) => (
  <svg {...base} strokeLinecap="butt" {...props}>
    <rect x="5" y="5" width="14" height="14" rx="1.5" />
  </svg>
);

export const RestoreIcon = (props) => (
  <svg {...base} strokeLinecap="butt" {...props}>
    <rect x="4" y="8" width="12" height="12" rx="1.5" />
    <path d="M8 4.5h10A1.5 1.5 0 0 1 19.5 6v10" />
  </svg>
);

export const CloseIcon = (props) => (
  <svg {...base} {...props}>
    <path d="m6 6 12 12M18 6 6 18" />
  </svg>
);

export const SunIcon = (props) => (
  <svg {...base} strokeWidth={2} {...props}>
    <circle cx="12" cy="12" r="4.2" />
    <path d="M12 2.5v2.4M12 19.1v2.4M2.5 12h2.4M19.1 12h2.4M5 5l1.7 1.7M17.3 17.3 19 19M19 5l-1.7 1.7M6.7 17.3 5 19" />
  </svg>
);

export const MoonIcon = (props) => (
  <svg viewBox="0 0 24 24" fill="currentColor" {...props}>
    <path d="M20.6 14.6A8.6 8.6 0 0 1 9.4 3.4 8.6 8.6 0 1 0 20.6 14.6Z" />
  </svg>
);

export const AlertIcon = (props) => (
  <svg {...base} {...props}>
    <path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z" />
    <path d="M12 9v4M12 17h.01" />
  </svg>
);

export const InfoIcon = (props) => (
  <svg {...base} {...props}>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 16v-4.5M12 8h.01" />
  </svg>
);

export const SearchIcon = (props) => (
  <svg {...base} {...props}>
    <circle cx="11" cy="11" r="7" />
    <path d="m20 20-3.6-3.6" />
  </svg>
);

export const ChevronLeftIcon = (props) => (
  <svg {...base} {...props}>
    <path d="m14 6-6 6 6 6" />
  </svg>
);

export const ChevronRightIcon = (props) => (
  <svg {...base} {...props}>
    <path d="m10 6 6 6-6 6" />
  </svg>
);

export const CheckIcon = (props) => (
  <svg {...base} strokeWidth={2.2} {...props}>
    <path d="m4 12.5 5.2 5.2L20 7" />
  </svg>
);

export const CopyIcon = (props) => (
  <svg {...base} {...props}>
    <rect x="9" y="9" width="12" height="12" rx="2" />
    <path d="M5 15a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2" />
  </svg>
);

export const EraserIcon = (props) => (
  <svg {...base} {...props}>
    <path d="M8.5 20.5 3.9 15.9a2 2 0 0 1 0-2.83l8.4-8.4a2 2 0 0 1 2.83 0l4.6 4.6a2 2 0 0 1 0 2.83L11.3 20.5Z" />
    <path d="M11 21h9M7.5 11.5 15 19" />
  </svg>
);

export const CpuIcon = (props) => (
  <svg {...base} {...props}>
    <rect x="6" y="6" width="12" height="12" rx="2" />
    <path d="M9.5 2.5v3.5M14.5 2.5v3.5M9.5 18v3.5M14.5 18v3.5M2.5 9.5H6M2.5 14.5H6M18 9.5h3.5M18 14.5h3.5" />
  </svg>
);

export const PowerIcon = (props) => (
  <svg {...base} {...props}>
    <path d="M12 3v9" />
    <path d="M18.4 6.6a9 9 0 1 1-12.8 0" />
  </svg>
);

export const LayersIcon = (props) => (
  <svg {...base} {...props}>
    <path d="m12 2.5 9 5-9 5-9-5 9-5Z" />
    <path d="m3 12.5 9 5 9-5M3 17l9 5 9-5" />
  </svg>
);

export const HomeIcon = (props) => (
  <svg {...base} {...props}>
    <path d="m3 10 9-7 9 7v10a2 2 0 0 1-2 2h-4v-7h-6v7H5a2 2 0 0 1-2-2z" />
  </svg>
);

/* Sprout — the Farming section. A seedling rather than a plant: the section is
   about growing something over time, not about what it grew into. */
/* Farming. A grid of cells, not a sprout: "farming" is grinding, not
   agriculture, and the tab's job is supervising many instances at once. The
   four cells are deliberately Home's InstrumentStrip shrunk to 24px, so the
   rail icon and the readout it leads to are the same picture. */
export const GridIcon = (props) => (
  <svg {...base} {...props}>
    <rect x="3" y="3" width="7.5" height="7.5" rx="1.6" />
    <rect x="13.5" y="3" width="7.5" height="7.5" rx="1.6" />
    <rect x="3" y="13.5" width="7.5" height="7.5" rx="1.6" />
    <rect x="13.5" y="13.5" width="7.5" height="7.5" rx="1.6" />
  </svg>
);

export const LockIcon = (props) => (
  <svg {...base} {...props}>
    <rect x="4" y="10.5" width="16" height="10.5" rx="2" />
    <path d="M8 10.5V7a4 4 0 0 1 8 0v3.5" />
  </svg>
);

export const ClockIcon = (props) => (
  <svg {...base} {...props}>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 7v5l3.2 1.9" />
  </svg>
);

export const HeartPulseIcon = (props) => (
  <svg {...base} {...props}>
    <path d="M20.4 5.6a5 5 0 0 0-7.1 0L12 6.9l-1.3-1.3a5 5 0 1 0-7.1 7.1l1.3 1.3L12 21l7.1-7a34 34 0 0 0 1.3-1.3 5 5 0 0 0 0-7.1z" />
    <path d="M3.5 12.5h3l1.5-2.5 2 4 1.5-3 1 1.5h3" />
  </svg>
);

export const SproutIcon = (props) => (
  <svg {...base} {...props}>
    <path d="M7 20h10" />
    <path d="M10 20c5.5-2.5.8-6.4 3-10" />
    <path d="M9.5 9.4c1.1.8 1.8 2.2 2.3 3.7-2 .4-3.5.4-4.8-.3-1.2-.6-2.3-1.9-3-4.2 2.8-.5 4.4 0 5.5.8z" />
    <path d="M14.1 6a7 7 0 0 0-1.1 4c1.9-.1 3.3-.6 4.3-1.4 1-1 1.6-2.3 1.7-4.6-2.7.1-4 1-4.9 2z" />
  </svg>
);

export const CheckSquareIcon = (props) => (
  <svg {...base} {...props}>
    <rect x="3" y="3" width="18" height="18" rx="3" />
    <path d="m8 12 3 3 5-6" />
  </svg>
);

/* Network. The rail's tab icon: rising signal bars, which read as
   "connection quality" rather than the generic globe a "Network" section
   usually gets — quality is exactly what the tab reports. */
export const SignalIcon = (props) => (
  <svg {...base} {...props}>
    <path d="M3 20v-3" />
    <path d="M8.5 20v-7" />
    <path d="M14 20v-11" />
    <path d="M19.5 20V4" />
  </svg>
);

/* One host, out there. Used per-row inside the Network tab. */
export const GlobeIcon = (props) => (
  <svg {...base} {...props}>
    <circle cx="12" cy="12" r="9" />
    <path d="M3 12h18" />
    <path d="M12 3a15 15 0 0 1 0 18a15 15 0 0 1 0-18z" />
  </svg>
);

/* The proxy: traffic taking a detour through somewhere else. */
export const RouteIcon = (props) => (
  <svg {...base} {...props}>
    <circle cx="5.5" cy="18.5" r="2.5" />
    <circle cx="18.5" cy="5.5" r="2.5" />
    <path d="M8 18.5h5a3.5 3.5 0 0 0 0-7h-2a3.5 3.5 0 0 1 0-7h5" />
  </svg>
);

/* Re-run the checks. */
export const RefreshIcon = (props) => (
  <svg {...base} {...props}>
    <path d="M21 12a9 9 0 1 1-2.6-6.4" />
    <path d="M21 4v5h-5" />
  </svg>
);

export const ChartIcon = (props) => (
  <svg {...base} {...props}>
    <path d="M3 3v16a2 2 0 0 0 2 2h16" />
    <path d="m7 15 3.5-4 3 2.5L18 8" />
  </svg>
);
