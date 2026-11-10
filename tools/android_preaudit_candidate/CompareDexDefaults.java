package io.github.ayamedog.fgostats.devtools;

import com.android.tools.smali.baksmali.Baksmali;
import com.android.tools.smali.baksmali.BaksmaliOptions;
import com.android.tools.smali.dexlib2.DexFileFactory;
import com.android.tools.smali.dexlib2.Opcodes;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Set;
import java.util.TreeSet;
import java.util.regex.Pattern;

/** Synthetic developer comparator. Not a release artifact. */
public final class CompareDexDefaults {
    private static final Pattern BOOL = Pattern.compile("^\\.field .*\\bstatic\\b .*:Z = false$");
    private static final Pattern REF = Pattern.compile("^\\.field .*\\bstatic\\b .*:(?:L[^; ]+;|\\[\\S+) = null$");

    static boolean equalExceptTrailingDefaults(List<String> left, List<String> right) {
        List<String> a = left.stream().filter(x -> !x.startsWith(".source ")).toList();
        List<String> b = right.stream().filter(x -> !x.startsWith(".source ")).toList();
        if (a.size() != b.size()) return false;
        boolean removed = false;
        for (int i = 0; i < a.size(); i++) {
            String x = a.get(i), y = b.get(i);
            if (!x.equals(y)) {
                if (!(BOOL.matcher(x).matches() || REF.matcher(x).matches()) ||
                        !y.equals(x.substring(0, x.lastIndexOf(" = ")))) return false;
                removed = true;
            } else if (removed && x.startsWith(".field ") && x.contains(" static ") && x.contains(" = ")) {
                return false;
            }
        }
        return true;
    }

    private static Set<String> files(Path root) throws Exception {
        Set<String> result = new TreeSet<>();
        try (var walk = Files.walk(root)) {
            for (Path p : walk.filter(Files::isRegularFile).toList()) result.add(root.relativize(p).toString());
        }
        return result;
    }

    private static void selfTest() {
        String yes = ".field private static ready:Z = false", no = ".field private static ready:Z";
        String ref = ".field private static cache:Ljava/lang/Object; = null";
        String nil = ".field private static cache:Ljava/lang/Object;";
        if (!equalExceptTrailingDefaults(List.of(yes, ref), List.of(no, nil)) ||
            equalExceptTrailingDefaults(List.of(yes, ".field static other:I = 7"),
                    List.of(no, ".field static other:I = 7")) ||
            equalExceptTrailingDefaults(List.of(".field static ready:I = false"), List.of(".field static ready:I")) ||
            equalExceptTrailingDefaults(List.of(".field static ready:Z = true"), List.of(no)) ||
            equalExceptTrailingDefaults(List.of(ref), List.of(".field private static cache:Ljava/lang/String;")))
            throw new IllegalStateException("SELF_TEST_FAILED");
        System.out.println("DEFAULT_COMPARISON_SELF_TEST_PASSED");
    }

    public static void main(String[] args) throws Exception {
        if (args.length == 1 && args[0].equals("--self-test")) { selfTest(); return; }
        if (args.length != 5) throw new IllegalArgumentException("INPUT_INVALID");
        Path root = Path.of(args[0]).toRealPath();
        Path first = Path.of(args[1]).toRealPath(), second = Path.of(args[2]).toRealPath();
        Path left = Path.of(args[3]).toAbsolutePath().normalize(), right = Path.of(args[4]).toAbsolutePath().normalize();
        if (!first.startsWith(root) || !second.startsWith(root) ||
                !left.getParent().toRealPath().startsWith(root) || !right.getParent().toRealPath().startsWith(root) ||
                Files.exists(left) || Files.exists(right)) throw new IllegalArgumentException("INPUT_INVALID");
        Files.createDirectory(left); Files.createDirectory(right);
        BaksmaliOptions options = new BaksmaliOptions();
        options.apiLevel = 24; options.debugInfo = false; options.sequentialLabels = true;
        if (!Baksmali.disassembleDexFile(DexFileFactory.loadDexFile(first.toFile(), Opcodes.forApi(24)), left.toFile(), 1, options) ||
            !Baksmali.disassembleDexFile(DexFileFactory.loadDexFile(second.toFile(), Opcodes.forApi(24)), right.toFile(), 1, options))
            throw new IllegalStateException("DISASSEMBLY_FAILED");
        Set<String> names = files(left);
        if (!names.equals(files(right))) throw new IllegalStateException("CLASS_SET_CHANGED");
        for (String name : names)
            if (!equalExceptTrailingDefaults(Files.readAllLines(left.resolve(name), StandardCharsets.UTF_8),
                    Files.readAllLines(right.resolve(name), StandardCharsets.UTF_8)))
                throw new IllegalStateException("CLASS_SEMANTICS_CHANGED");
        System.out.println("DEX_SEMANTICS_MATCHED " + names.size());
    }
}
