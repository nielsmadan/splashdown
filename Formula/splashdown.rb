# Copy this file to a Homebrew tap repo (named `homebrew-tap` or similar)
# under `Formula/splashdown.rb`. Users then install with:
#
#   brew install <user>/<tap>/splashdown
#
# Update `url` to the GitHub release tarball and `sha256` after each tag:
#
#   curl -L https://github.com/USER/splashdown/archive/refs/tags/v0.1.0.tar.gz | shasum -a 256

class Splashdown < Formula
  include Language::Python::Virtualenv

  desc "Per-checkout iOS sims and dev ports for mobile development on git worktrees"
  homepage "https://github.com/USER/splashdown"
  url "https://github.com/USER/splashdown/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "REPLACE_WITH_TARBALL_SHA256"
  license "MIT"
  head "https://github.com/USER/splashdown.git", branch: "main"

  depends_on "python@3.13"

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "spd", shell_output("#{bin}/spd --help")
    # Init + provision in a scratch dir round-trips.
    Dir.chdir(testpath) do
      system "git", "init", "-q"
      system bin/"spd", "init", "--preset=minimal"
      assert_predicate testpath/".worktree.toml", :exist?
      ENV["XDG_STATE_HOME"] = testpath/"state"
      system bin/"spd"
      assert_predicate testpath/"mise.local.toml", :exist?
    end
  end
end
