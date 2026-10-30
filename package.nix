{ lib, stdenvNoCC, python3, qt6, mpv, makeWrapper, makeDesktopItem, copyDesktopItems }:
stdenvNoCC.mkDerivation {
  pname = "imagesearcher";
  version = "2.0.0";
  src = lib.fileset.toSource { root = ./.; fileset = lib.fileset.unions [ ./imagesearcher.py ./imagesearcher_app ]; };
  nativeBuildInputs = [ makeWrapper copyDesktopItems ];
  desktopItems = [
    (makeDesktopItem {
      name = "imagesearcher";
      desktopName = "Image Searcher";
      comment = "Booru (any site) and Pixiv browser";
      exec = "imagesearcher";
      icon = "applications-graphics";
      categories = [ "Graphics" "Network" ];
    })
  ];
  installPhase = ''
    runHook preInstall
    install -Dm644 imagesearcher.py $out/share/imagesearcher/imagesearcher.py
    cp -r imagesearcher_app $out/share/imagesearcher/
    makeWrapper ${python3.withPackages (ps: [ ps.pyside6 ])}/bin/python $out/bin/imagesearcher \
      --add-flags $out/share/imagesearcher/imagesearcher.py \
      --prefix QT_PLUGIN_PATH : ${qt6.qtwayland}/${qt6.qtbase.qtPluginPrefix} \
      --suffix PATH : ${lib.makeBinPath [ mpv ]}
    runHook postInstall
  '';
  meta = {
    description = "Booru (any Danbooru / Gelbooru / Moebooru / e621 / Philomena site) and Pixiv browser and downloader";
    homepage = "https://github.com/acquevoor/imagesearcher";
    mainProgram = "imagesearcher";
    platforms = lib.platforms.linux;
  };
}
