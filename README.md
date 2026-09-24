# Age of Joy Texture Checker

A quick and dirty Windows tool for optimizing cabinets for the cool VR arcade simulator **Age of Joy**. Just open a cabinet and it lists every texture that might break the texturing size rules, shows you what to change, and saves a copy of the cabinet with the textures resized. Your original cabinet is never changed. Wow!

## Download

1. Go to the [Releases page](https://github.com/mcwild77/AOJ-TextureCheck/releases/latest) and download `TextureChecker.zip`.
2. Right-click the zip, choose **Extract All**, and open the extracted folder.
3. Double-click `TextureChecker.exe`. It can take a few seconds to open.

There's nothing to install. It runs on Windows 10 and 11. Steam Deck? Er, maybe.

### "Windows protected your PC"

The first time you open the app, Windows will probably annoy you with a blue box that says **Windows protected your PC**. This pops because the app isn't signed with a paid code-signing certificate and Windows hasn't seen it downloaded many times yet and it's some stuff I just kicked out.

To open it anyway:

1. Click **More info**.
2. Click **Run anyway**.

You should only need to do this the first time.

Your browser may also warn that the file "isn't commonly downloaded". Well, duh, it's a VR arcade cabinet optimization tool for a bunch of sickos like me. Choose **Keep**. Some antivirus programs wrongly flag apps built the way this one is (with PyInstaller). The full source code is in this repository if you want to check it or build it yourself (builds on Mac!).

## Before you do ANYTHING...
You should read the Age of Joy [Cabinet building best practices](https://curif.github.io/AgeOfJoyQuartzDocumentation/Documents/Cabinets/Cabinet-building-best-practices#choosing-good-texture-sizes---the-powers-of-two) guide. Seriously.

## How to use it

1. Click **Open cabinet (.zip)...** or **Open cabinet (folder)...** and pick your cabinet.
2. Check the table. Rows marked **Fix** need some tweaks or need resizing. 
3. Click a row to see that texture. With **Auto Focus** ticked, the 3D view zooms in to the part of the cabinet that uses it. Drag the 3D view to rotate it, and scroll to zoom.
4. The **Resize to** boxes start at the recommended size. **This is algorithmically determined and not the law!** You can go lower if need be -- the update in the 3D window shows how it will look at the resized resolution, so see how low you can go before it's perceptibly worse. 
5. Type a name in **Save as** (it starts as `<your cabinet>_optimized`), then click **Export to Zip** or **Export to New Folder**.
6. Using something like a 2000x2000 all-black texture will get you sent to 3D Artist Jail. You can resize it to 8x8 pixels but even that's not really acceptable, just pop into the YAML and change the part to COLOR and avoid the extra wasted texture read entirely. The 3D chipset in the Quest hardware needs all the help it can get.

The export copies the whole cabinet. Every texture that shows a **Recommended size** gets resized to it, unless you overrode it with a different size in the **Resize to** boxes. Everything else is copied as it is. When the export finishes, the app tells you how much in-game memory you saved.

The totals at the top add up the texture size and in-game size for the whole cabinet.

## What it checks

The rules come from the texture section of the Age of Joy [Cabinet building best practices](https://curif.github.io/AgeOfJoyQuartzDocumentation/Documents/Cabinets/Cabinet-building-best-practices#choosing-good-texture-sizes---the-powers-of-two) guide.

## Bonus Feature

If your cabinet uses a custom screen (`crt: type: custom`), the app also measures the screen and shows a red warning when it isn't 4:3. Most arcade games are 4:3, so a square or widescreen screen is usually a mistake.

## Good to know

- **Your original cabinet is never changed.** Exports always go to a new folder or zip, and the app won't save over the cabinet you opened.
- **Resizing doesn't actually change the aspect ratio (the shape of the texture) in 3D.** When a 1000x300 texture becomes 1024x256, it looks squashed in the texture preview window, but looks fine in 3D.

- **The app's suggestions only ever shrink textures.** A texture that isn't a power of two is rounded down (773 wide becomes 512), never up.
- **Not checked:** textures stored inside `.glb` model files.

## For developers

You need Python 3.10 or newer.

```
pip install -r requirements.txt
python run.py                                   # launch the app
python -m unittest discover -s tests -t . -v    # run the tests
```

On macOS, use `python3` in place of `python`.

To build the Windows exe, run `build_windows.bat` on Windows. It builds inside a private virtual environment (`build\venv`) so only the packages in `requirements.txt` get bundled, and writes `dist\TextureChecker.exe`. PyInstaller can't cross-compile, so the exe has to be built on Windows.

The code lives in `texturecheck/`:

- `rules.py`: the size rules and thresholds. Keep it in sync with [docs/texture-standards.md](docs/texture-standards.md).
- `cabinet.py`: opens a cabinet and checks each texture.
- `resize.py`: exports a copy of the cabinet with textures resized.
- `sources.py`: reads a cabinet from a zip or a folder.
- `preview3d.py`: the 3D preview and the custom-screen 4:3 check.
- `gui.py`: the window.
