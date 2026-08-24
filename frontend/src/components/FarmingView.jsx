/* Farming: scheduled, unattended play across accounts.

   Deliberately empty for now — the section exists so the navigation is
   settled before the machinery lands, and so nothing else has to move when it
   does. Everything a farm needs already exists elsewhere in the app (farming
   launch mode, autoexec, multi-account launch); what belongs here is the part
   that runs them on a plan rather than on a click. */

import { SproutIcon } from "./icons.jsx";

export default function FarmingView({ active }) {
  return (
    <div className={`min-h-0 flex-1 overflow-y-auto px-5 py-5 ${active ? "" : "hidden"}`}>
      <div className="animate-rise mx-auto flex w-full max-w-[1080px] flex-col gap-6">
        <div className="flex flex-col items-center gap-3 px-4 py-24 text-center">
          <SproutIcon className="h-7 w-7 text-ink-3" strokeWidth={1.4} />
          <div>
            <p className="text-[13px] font-medium text-ink">Farming</p>
            <p className="mx-auto mt-1 max-w-[40ch] text-[11.5px] leading-relaxed text-ink-3">
              Nothing here yet.
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
