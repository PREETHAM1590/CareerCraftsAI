"use client";
import { useCompanion } from "@/components/companion/CompanionProvider";
import { CompanionPet } from "@/components/companion/CompanionPet";
import { SettingsNav } from "@/components/settings/SettingsNav";
import { Bezel, PageHero, Screen } from "@/components/vanguard";
import Link from "next/link";

export default function CompanionSettingsPage() {
  const { preferences, updatePreferences, loaded, saveError, activity } = useCompanion();
  return <Screen><div className="space-y-6 md:space-y-8">
    <PageHero eyebrow="Companion" title="A little company." accent="Make it yours." description="Your floating companion follows you around CareerCraft, showing real task activity and letting you know when you're needed." />
    <SettingsNav />
    {!loaded ? <p role="status">Loading companion preferences…</p> : <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,1fr)_320px]">
      <Bezel coreClassName="space-y-7 p-6 sm:p-8">
        <div><h2 className="text-lg font-semibold">Your companion</h2><p className="mt-2 text-sm text-muted-foreground">Changes apply immediately and are saved for your account in this browser. Drag the floating pet to move it; it returns to your chosen corner after a refresh.</p></div>
        <label className="block text-sm font-medium">Name<input className="companion-input mt-2" maxLength={24} value={preferences.name} onChange={event => updatePreferences({ ...preferences, name: event.target.value })} /></label>
        <fieldset><legend className="mb-3 text-sm font-medium">Character</legend><div className="grid grid-cols-2 gap-3 sm:grid-cols-4">{(["mint", "blue", "orange", "purple"] as const).map(character => <button key={character} aria-pressed={preferences.character === character} className="companion-choice" onClick={() => updatePreferences({ ...preferences, character })}><span className="mx-auto block w-20"><CompanionPet preferences={{ ...preferences, character, motion: false }} /></span><span className="mt-2 block">{character}</span></button>)}</div></fieldset>
        <fieldset><legend className="mb-3 text-sm font-medium">Size</legend><div className="flex gap-3">{(["small", "medium", "large"] as const).map(size => <button key={size} className="companion-choice" aria-pressed={preferences.size === size} onClick={() => updatePreferences({ ...preferences, size })}>{size}</button>)}</div></fieldset>
        <fieldset><legend className="mb-3 text-sm font-medium">Screen corner</legend><div className="flex gap-3">{(["left", "right"] as const).map(corner => <button key={corner} className="companion-choice" aria-pressed={preferences.corner === corner} onClick={() => updatePreferences({ ...preferences, corner })}>Bottom {corner}</button>)}</div></fieldset>
        <label className="flex items-start gap-3 text-sm"><input className="mt-1" type="checkbox" checked={preferences.motion} onChange={event => updatePreferences({ ...preferences, motion: event.target.checked })} /><span><span className="block font-medium">Animate companion</span><span className="mt-1 block text-muted-foreground">Gentle movement reflects task status. Reduced-motion preferences are respected.</span></span></label>
        <label className="flex items-start gap-3 text-sm"><input className="mt-1" type="checkbox" checked={preferences.visible} onChange={event => updatePreferences({ ...preferences, visible: event.target.checked })} /><span><span className="block font-medium">Show floating companion</span><span className="mt-1 block text-muted-foreground">Display it across the app. Your tasks continue when it's hidden.</span></span></label>
        {saveError ? <p role="alert" className="text-sm text-destructive">{saveError}</p> : <p role="status" className="text-xs text-muted-foreground">Preferences saved in this browser.</p>}
      </Bezel>
      <Bezel coreClassName="p-6 text-center"><p className="text-xs font-medium text-muted-foreground">YOUR COMPANION</p><div className="mx-auto my-5 w-44"><CompanionPet preferences={preferences} state={activity.state} /></div><h2 className="text-xl font-semibold">{preferences.name}</h2><p className="mt-2 text-sm text-muted-foreground">{activity.title}</p><p className="mt-5 text-xs leading-5 text-muted-foreground">Click the floating pet to see task details, open chat, or review an action.</p><Link href="/nova" className="mt-5 inline-block text-sm text-primary underline underline-offset-4">Back to Nova</Link><p className="mt-6 text-[11px] text-muted-foreground">Characters from <a href="https://github.com/CopilotKit/OpenDots" target="_blank" rel="noreferrer" className="underline">OpenDots</a> · <a href="/companions/opendots/LICENSE.txt" target="_blank" rel="noreferrer" className="underline">MIT license</a></p></Bezel>
    </div>}
  </div></Screen>;
}
