{ lib, stdenvNoCC, python3, qt6, mpv, makeWrapper, makeDesktopItem, copyDesktopItems }:
stdenvNoCC.mkDerivation {
  pname = "imagesearcher";
  version = "0.1.0";
  src = lib.fileset.toSource { root = ./.; fileset = ./imagesearcher.py; };
  nativeBuildInputs = [ makeWrapper copyDesktopItems ];
  desktopItems = [
    (makeDesktopItem {
      name = "imagesearcher";
      desktopName = "Image Searcher";
      comment = "Danbooru / Gelbooru / Rule34 / Pixiv browser";
      exec = "imagesearcher";
      icon = "applications-graphics";
      categories = [ "Graphics" "Network" ];
    })
  ];
  installPhase = ''
    runHook preInstall
    install -Dm644 imagesearcher.py $out/share/imagesearcher/imagesearcher.py
    makeWrapper ${python3.withPackages (ps: [ ps.pyside6 ])}/bin/python $out/bin/imagesearcher \
      --add-flags $out/share/imagesearcher/imagesearcher.py \
      --prefix QT_PLUGIN_PATH : ${qt6.qtwayland}/${qt6.qtbase.qtPluginPrefix} \
      --suffix PATH : ${lib.makeBinPath [ mpv ]}
    runHook postInstall
  '';
  meta = {
    description = "Desktop browser/downloader for Danbooru, Gelbooru, Rule34.xxx and Pixiv";
    homepage = "https://github.com/acquevoor/imagesearcher";
    mainProgram = "imagesearcher";
    platforms = lib.platforms.linux;
  };
}
