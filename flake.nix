{
  description = "Desktop browser/downloader for Danbooru, Gelbooru, Rule34.xxx and Pixiv";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      forAll = f: nixpkgs.lib.genAttrs [ "x86_64-linux" "aarch64-linux" ] (s: f nixpkgs.legacyPackages.${s});
    in
    {
      packages = forAll (pkgs: { default = pkgs.callPackage ./package.nix { }; });
      apps = forAll (pkgs: {
        default = { type = "app"; program = "${self.packages.${pkgs.stdenv.hostPlatform.system}.default}/bin/imagesearcher"; };
      });
      checks = forAll (pkgs: {
        selftest = pkgs.runCommand "imagesearcher-selftest" { } ''
          ${self.packages.${pkgs.stdenv.hostPlatform.system}.default}/bin/imagesearcher --selftest && touch $out
        '';
      });
    };
}
