package app
import org.apache.commons.lang3.StringUtils
fun greet(): String = StringUtils.capitalize(lib.toJson(lib.Item("x")))
fun main() = println(greet())
